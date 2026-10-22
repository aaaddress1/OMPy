"""Native Chat Completions adapter for the Windows Host API."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
from openai import OpenAI
from pydantic import BaseModel, Field

from minisweagent.context_compaction import CompactContextManager, ContextCompactionError
from minisweagent.context_manager import ContextManager
from minisweagent.context_store import CONTEXT_TOOL
from minisweagent.exceptions import FormatError
from minisweagent.models.utils.actions_toolcall import parse_toolcall_actions
from minisweagent.models.utils.chat_stream import collect_completion

POWERSHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "powershell",
        "description": "執行 Windows PowerShell 5.1 命令或 .exe，取得輸出與退出碼。版本：$PSVersionTable.PSVersion；前 N 筆：Select-Object -First N。不確定的命令先用 Get-Command 查證。",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "完整的 PowerShell 命令"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


class WindowsHostConfig(BaseModel):
    model_name: str = "your-model-name"
    base_url: str = "http://127.0.0.1:11434/v1"
    context_window: int = Field(default=1048576, gt=0)
    max_output_tokens: int = Field(default=8192, gt=0)
    reasoning_effort: Literal["none", "low", "high"] = "none"
    context_policy: Literal["compact", "sliding", "append", "memory"] = "compact"


class WindowsHostModel:
    def __init__(self, *, api_key: str, http_client: httpx.Client, **kwargs):
        self.config = WindowsHostConfig(**kwargs)
        if self.config.max_output_tokens + 1024 >= self.config.context_window:
            raise ValueError("上下文容量必須大於輸出上限與預留空間。")
        self.client = OpenAI(base_url=self.config.base_url, api_key=api_key, http_client=http_client, max_retries=1)
        context_options = {
            "context_window": self.config.context_window, "max_output_tokens": self.config.max_output_tokens,
        }
        self.context = (
            CompactContextManager(summarize=self.summarize, **context_options)
            if self.config.context_policy == "compact"
            else ContextManager(policy=self.config.context_policy, **context_options)
        )
        self.run_dir = Path(".runs")
        self.request_count = 0
        self.on_delta: Callable[[str, str], None] | None = None

    def close(self) -> None:
        if isinstance(self.context, CompactContextManager):
            self.context.close()

    def summarize(self, messages: list[dict], path: Path) -> str:
        request = {
            "model": self.config.model_name, "messages": messages,
            "max_completion_tokens": min(2048, self.config.max_output_tokens, max(128, self.context.input_budget // 8)),
            "reasoning_effort": "none",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        # Own the worker's client so session shutdown cannot close an in-flight summary's transport.
        with OpenAI(
            base_url=str(self.client.base_url), api_key=self.client.api_key, max_retries=0,
            http_client=httpx.Client(trust_env=False, timeout=self.client.timeout),
        ) as client:
            response = client.chat.completions.create(**request)
        path.write_text(json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not (choice.message.content or "").strip():
            raise ContextCompactionError("摘要未完整產生；原始回應已保留。")
        return choice.message.content

    def notes(self) -> list[str] | None:
        """memory policy: the agent appends notes to a session file with PowerShell; every window shows them."""
        if self.config.context_policy not in {"memory", "compact"}:
            return None
        path = (self.run_dir.parent / "notes.md").resolve()
        if self.config.context_policy == "compact":
            return [f"舊筆記檔：{path}；需要時用 PowerShell 分段讀取。"] if path.exists() else None
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        return [f"（筆記檔：{path}。用 PowerShell 以 Add-Content -Encoding utf8 每行附加一則；重要發現與進度請記在這裡）"] + [
            line for line in lines if line.strip()
        ]

    def query(self, messages: list[dict], **kwargs) -> dict:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.request_count += 1
        tools = [POWERSHELL_TOOL] + ([CONTEXT_TOOL] if self.config.context_policy == "compact" else [])
        request = {
            "model": self.config.model_name,
            "messages": self.context.prepare(
                messages, tools, archive=self.run_dir.parent, notes=self.notes()
            ),
            "tools": tools,
            "parallel_tool_calls": False,
            "max_completion_tokens": self.config.max_output_tokens,
            "reasoning_effort": self.config.reasoning_effort,
            **kwargs,
        }
        if self.on_delta is not None:
            request |= {"stream": True, "stream_options": {"include_usage": True}}
        (self.run_dir / f"request-{self.request_count:04d}.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if self.on_delta is not None:
            with self.client.chat.completions.create(**request) as stream:
                response = collect_completion(stream, self.on_delta)
        else:
            response = self.client.chat.completions.create(**request)
        if response.usage:
            self.context.observe(response.usage.prompt_tokens)
        message = response.choices[0].message.model_dump(exclude_none=True)
        # Some compatible servers return reasoning_content as an extension.
        # Preserve the full response on disk; replay standard chat fields only.
        message = {k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}}
        extra = {"cost": 0.0, "response": response.model_dump(mode="json"), "actions": []}
        extra["streamed"] = self.on_delta is not None
        if response.choices[0].message.tool_calls:
            extra["actions"] = self.parse_actions(response.choices[0].message.tool_calls)
        elif response.choices[0].finish_reason == "length" or not (message.get("content") or "").strip():
            raise FormatError(
                {
                    "role": "user",
                    "content": "輸出被截斷或為空。請簡短回答或呼叫工具，繼續完成任務。",
                    "extra": {"cost": 0.0},
                }
            )
        else:
            message["role"] = "exit"
            extra.update(exit_status="Submitted", submission=message["content"])
        return message | {"extra": extra}

    def parse_actions(self, calls: list) -> list[dict]:
        actions = []
        for call in calls:
            memory = call.function.name == "context" and self.config.context_policy == "compact"
            canonical = call.model_copy(update={"function": call.function.model_copy(update={
                "name": "bash" if memory or call.function.name == "powershell" else call.function.name
            })})
            action = parse_toolcall_actions([canonical], command_key=None if memory else "command",
                format_error_template="工具格式錯誤：{{ error }}。powershell 使用 command 字串；context 使用 operation 及其參數。") [0]
            if memory:
                action = action | {"context": action["command"], "command": json.dumps(action["command"], ensure_ascii=False)}
            elif not isinstance(action["command"], str) or not action["command"].strip():
                raise FormatError({"role": "user", "content": "PowerShell command 必須是非空字串。"})
            actions.append(action)
        return actions

    def format_message(self, **kwargs) -> dict:
        return kwargs

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        return [
            {
                "role": "tool",
                "tool_call_id": action["tool_call_id"],
                "content": json.dumps(output, ensure_ascii=False),
                "extra": {"returncode": output["returncode"], "archive": output["archive"]},
            }
            for action, output in zip(message.get("extra", {}).get("actions", []), outputs)
        ]

    def get_template_vars(self, **kwargs) -> dict:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {"info": {"config": {"model": self.config.model_dump(), "model_type": f"{__name__}.WindowsHostModel"}}}
