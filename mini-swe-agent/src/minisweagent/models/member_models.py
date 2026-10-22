"""Optional member transports; OMPy retains its own agent loop and context store."""

import json
import threading
import time
from uuid import uuid4

import httpx
from openai.types.chat import ChatCompletion

from minisweagent.context_compaction import ContextCompactionError
from minisweagent.models.chatgpt_auth import RESOURCE, RUNTIME, ChatGPTAuth
from minisweagent.models.windows_host import WindowsHostModel

SELECTION = RUNTIME / "provider.json"


def chat_completion(model: str, text: str, calls: list, usage: dict, *, finished: bool = True) -> ChatCompletion:
    return ChatCompletion.model_validate({"id": "ompy-" + uuid4().hex, "object": "chat.completion",
        "created": int(time.time()), "model": model, "choices": [{"index": 0,
            "finish_reason": ("tool_calls" if calls else "stop") if finished else "length",
            "message": {"role": "assistant", "content": text, "tool_calls": calls or None}}], "usage": usage})


def responses_request(request: dict) -> dict:
    instructions, items = [], []
    for message in request["messages"]:
        role, content = message["role"], message.get("content") or ""
        if role == "system":
            instructions.append(content)
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content})
        else:
            if content:
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"], "namespace": "ompy",
                    "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    result = {"model": request["model"], "instructions": "\n\n".join(instructions), "input": items,
        "stream": True, "store": False, "reasoning": {"effort": request.get("reasoning_effort", "low")}}
    if request.get("tools"):
        result["tools"] = [{"type": "namespace", "name": "ompy", "description": "OMPy local tools",
            "tools": [{"type": "function", **tool["function"], "strict": False} for tool in request["tools"]]}]
        result["parallel_tool_calls"] = False
    return result


def collect_responses(events, model: str, notify=None) -> ChatCompletion:
    completed = None
    output = {}
    text_deltas = []
    for event in events:
        kind = event.get("type")
        if kind in {"error", "response.failed", "response.incomplete"}:
            raise ContextCompactionError("ChatGPT 回應未完成；停止本輪，不重試或切換到付費 API。")
        if kind == "response.output_text.delta":
            text_deltas.append(event.get("delta", ""))
            if notify:
                notify("content", event.get("delta", ""))
        if kind == "response.reasoning_summary_text.delta" and notify:
            notify("reasoning_content", event.get("delta", ""))
        if kind == "response.output_item.done":
            output[event["output_index"]] = event["item"]
        if kind == "response.completed":
            completed = event["response"]
    if not completed or completed.get("status") != "completed":
        raise ContextCompactionError("ChatGPT 串流中斷，未收到完成事件。")
    text, calls = [], []
    for item in completed.get("output") or [output[index] for index in sorted(output)]:
        if item["type"] == "message":
            text.extend(part.get("text", "") for part in item.get("content", []) if part["type"] == "output_text")
        if item["type"] == "function_call":
            if item.get("namespace") != "ompy":
                raise ValueError("ChatGPT 回傳未知工具命名空間。")
            calls.append({"id": item["call_id"], "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]}})
    if not any(text):
        text = text_deltas
    if not any(text) and not calls:
        raise ContextCompactionError("ChatGPT 完成事件缺少文字與工具結果；停止本輪。")
    usage = completed.get("usage") or {}
    return chat_completion(model, "".join(text), calls, {"prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens_details": usage.get("input_tokens_details"),
        "completion_tokens_details": usage.get("output_tokens_details")})


class MemberModel(WindowsHostModel):
    def __init__(self, *, request_limit: int = 3, **kwargs):
        super().__init__(api_key="member-managed", **kwargs)
        self.request_limit, self.member_calls = request_limit, 0
        self._budget_lock = threading.Lock()

    def reserve_request(self):
        with self._budget_lock:
            if not 1 <= self.request_limit <= 100 or self.member_calls >= self.request_limit:
                raise ContextCompactionError("已達本次會員請求上限；停止本輪。背景摘要也計入上限。")
            self.member_calls += 1


class ChatGPTMemberModel(MemberModel):
    provider_label = "ChatGPT 會員"

    def __init__(self, *, auth: ChatGPTAuth | None = None, **kwargs):
        super().__init__(**kwargs)
        self.auth = auth or ChatGPTAuth()

    def complete(self, request: dict, *, summary: bool = False):
        payload = responses_request(request)
        token = self.auth.token()
        self.reserve_request()
        # A separate client for each request also isolates background compaction.
        with httpx.Client(trust_env=False, timeout=60) as http:
            with http.stream("POST", RESOURCE + "/responses", json=payload,
                headers={"Authorization": "Bearer " + token, "User-Agent": "OMPy/1.0"}) as response:
                if response.status_code >= 400:
                    raise ContextCompactionError(f"ChatGPT HTTP {response.status_code}；停止，不重試或改用其他帳戶。")
                events = (json.loads(line[5:].strip()) for line in response.iter_lines()
                          if line.startswith("data:") and line[5:].strip() != "[DONE]")
                return collect_responses(events, request["model"], None if summary else self.on_delta)
