"""ACP stdio entry point: expose the Windows agent to editors such as VS Code.

stdout carries the protocol, so launch with MSWEA_SILENT_STARTUP=1 and never print.
"""

import asyncio
import json
import logging
import sys
import threading
from typing import Literal
from uuid import uuid4

import httpx
import typer
from acp import (
    PROTOCOL_VERSION,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)
from acp.helpers import update_available_commands
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AvailableCommand,
    AvailableCommandInput,
    Implementation,
    PermissionOption,
    ToolCallUpdate,
    UnstructuredCommandInput,
)

from minisweagent import __version__
from minisweagent.agents.default import DefaultAgent
from minisweagent.context_compaction import CompactContextManager, ContextCompactionError
from minisweagent.context_manager import ContextWindowExceeded
from minisweagent.environments.powershell import PowerShellEnvironment
from minisweagent.exceptions import InterruptAgentFlow
from minisweagent.run.windows_console import PROJECT_ROOT, SYSTEM_PROMPT, create_model

app = typer.Typer(pretty_exceptions_enable=False)
STOP_REASONS = {
    "Submitted": "end_turn",
    "LimitsExceeded": "max_turn_requests",
    "Cancelled": "cancelled",
    "ContextWindowExceeded": "max_tokens",
}
COMMANDS = [
    AvailableCommand(name="new", description="清除目前對話；磁碟上的紀錄保留"),
    AvailableCommand(
        name="reasoning",
        description="切換推理程度",
        input=AvailableCommandInput(UnstructuredCommandInput(hint="none | low | high")),
    ),
]
PERMISSION_OPTIONS = [
    PermissionOption(option_id="allow", name="允許", kind="allow_once"),
    PermissionOption(option_id="allow_always", name="本次對話都允許", kind="allow_always"),
    PermissionOption(option_id="reject", name="拒絕", kind="reject_once"),
]


class Session:
    def __init__(self, cwd: str, http_client: httpx.Client, model_overrides: dict):
        self.id = uuid4().hex
        self.cwd = cwd
        self.model = create_model(http_client, **model_overrides)
        self.run_dir = PROJECT_ROOT / ".runs" / self.id
        self.turn = 0
        self.allow_all = False
        self.cancelled = threading.Event()
        self.prompt_lock = asyncio.Lock()


class BridgeAgent(DefaultAgent):
    """Runs in a worker thread; forwards progress to the editor and asks before each command."""

    def __init__(self, *args, session: Session, conn, loop: asyncio.AbstractEventLoop, **kwargs):
        super().__init__(*args, **kwargs)
        self.session, self.conn, self.loop = session, conn, loop
        self.model.on_delta = self.notify_delta
        if isinstance(self.model.context, CompactContextManager):
            self.model.context.check_cancelled = self.check_cancelled
            self.model.context.on_event = lambda text: self.notify(update_agent_message_text(f"\n\n[上下文] {text}\n\n"))

    def notify_delta(self, field: str, text: str) -> None:
        self.check_cancelled()
        self.notify(update_agent_thought_text(text) if field == "reasoning_content" else update_agent_message_text(text))

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def notify(self, update) -> None:
        self.call(self.conn.session_update(self.session.id, update))

    def check_cancelled(self) -> None:
        if self.session.cancelled.is_set():
            raise InterruptAgentFlow(
                {"role": "exit", "content": "", "extra": {"exit_status": "Cancelled", "submission": ""}}
            )

    def add_messages(self, *messages: dict) -> list[dict]:
        for message in messages:
            extra = message.get("extra", {})
            if message["role"] == "assistant" and message.get("content") and not extra.get("streamed"):
                self.notify(update_agent_message_text(message["content"]))
            if message["role"] == "exit" and extra.get("exit_status") == "Submitted":
                if not extra.get("streamed"):
                    self.notify(update_agent_message_text(message["content"]))
            elif message["role"] == "exit" and extra.get("exit_status") != "Cancelled":
                self.notify(update_agent_message_text(f"\n\n本輪停止（{extra.get('exit_status')}）：{message['content']}"))
            for action in extra.get("actions", []):
                self.notify(
                    start_tool_call(
                        action["tool_call_id"],
                        {"search": "搜尋對話證據", "read": "回讀原始證據", "remember": "更新記憶筆記", "forget": "移除過時筆記"}.get(
                            action.get("context", {}).get("operation"), action["command"].strip().splitlines()[0][:120]),
                        kind="other" if "context" in action else "execute",
                        status="pending",
                        raw_input=action.get("context", {"command": action["command"]}),
                    )
                )
            if message["role"] == "tool":
                output = json.loads(message["content"])
                text = output["output"] + (f"\n{output['exception_info']}" if output.get("exception_info") else "")
                self.notify(
                    update_tool_call(
                        message["tool_call_id"],
                        status="completed" if output["returncode"] == 0 else "failed",
                        content=[tool_content(text_block(text or f"（無輸出，退出碼 {output['returncode']}）"))],
                        raw_output=output,
                    )
                )
        return super().add_messages(*messages)

    def query(self) -> dict:
        self.check_cancelled()
        return super().query()

    def allowed(self, action: dict) -> bool:
        if "context" in action:
            return True
        if self.session.allow_all:
            return True
        response = self.call(
            self.conn.request_permission(
                session_id=self.session.id,
                tool_call=ToolCallUpdate(
                    tool_call_id=action["tool_call_id"],
                    title=action["command"].strip().splitlines()[0][:120],
                    kind="execute",
                    status="pending",
                    raw_input={"command": action["command"]},
                ),
                options=PERMISSION_OPTIONS,
            )
        )
        if not isinstance(response.outcome, AllowedOutcome) or response.outcome.option_id == "reject":
            return False
        self.session.allow_all |= response.outcome.option_id == "allow_always"
        return True

    def execute_actions(self, message: dict) -> list[dict]:
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            self.check_cancelled()
            if not self.allowed(action):
                outputs.append({"output": "使用者拒絕執行此命令。", "returncode": -1, "exception_info": "", "archive": ""})
                continue
            self.notify(update_tool_call(action["tool_call_id"], status="in_progress"))
            outputs.append(self.env.execute(action))
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))


class WindowsAcpAgent:
    def __init__(self, http_client: httpx.Client, *, step_limit: int, command_timeout: int, model_overrides: dict):
        self.http_client = http_client
        self.step_limit = step_limit
        self.command_timeout = command_timeout
        self.model_overrides = model_overrides
        self.sessions: dict[str, Session] = {}

    def on_connect(self, conn) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kwargs) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(name="ompy", title="Oh My PiPy (OMPy)", version=__version__),
        )

    async def new_session(self, cwd: str, **kwargs) -> NewSessionResponse:
        session = Session(cwd, self.http_client, self.model_overrides)
        self.sessions[session.id] = session
        asyncio.get_running_loop().call_later(
            0.1, lambda: asyncio.ensure_future(self.conn.session_update(session.id, update_available_commands(COMMANDS)))
        )
        return NewSessionResponse(session_id=session.id)

    async def cancel(self, session_id: str, **kwargs) -> None:
        self.sessions[session_id].cancelled.set()

    async def prompt(self, session_id: str, prompt: list, **kwargs) -> PromptResponse:
        async with self.sessions[session_id].prompt_lock:
            return await self._prompt(session_id, prompt, **kwargs)

    async def _prompt(self, session_id: str, prompt: list, **kwargs) -> PromptResponse:
        session = self.sessions[session_id]
        text = "\n\n".join(
            block.text
            if block.type == "text"
            else f"[附加檔案 {block.uri}]"
            if block.type == "resource_link"
            else f"[附加檔案 {block.resource.uri}]\n{getattr(block.resource, 'text', '')}"
            for block in prompt
            if block.type in {"text", "resource_link", "resource"}
        ).strip()
        if text == "/new":
            session.model.context.clear()
            await self.conn.session_update(session_id, update_agent_message_text("已清除目前對話；磁碟上的紀錄保留。"))
            return PromptResponse(stop_reason="end_turn")
        if text.startswith("/reasoning"):
            effort = text.removeprefix("/reasoning").strip()
            if effort in {"none", "low", "high"}:
                session.model.config.reasoning_effort = effort
            await self.conn.session_update(
                session_id,
                update_agent_message_text(
                    f"推理程度：{session.model.config.reasoning_effort}"
                    if effort in {"none", "low", "high", ""}
                    else "可使用：/reasoning none、/reasoning low、/reasoning high"
                ),
            )
            return PromptResponse(stop_reason="end_turn")
        session.cancelled.clear()
        status = await asyncio.to_thread(self.run_turn, session, text, asyncio.get_running_loop())
        return PromptResponse(stop_reason=STOP_REASONS.get(status, "end_turn"))

    def run_turn(self, session: Session, task: str, loop: asyncio.AbstractEventLoop) -> str:
        session.turn += 1
        session.model.run_dir = session.run_dir / f"turn-{session.turn:04d}"
        session.model.request_count = 0
        agent = BridgeAgent(
            session.model,
            PowerShellEnvironment(output_dir=session.model.run_dir, cwd=session.cwd, timeout=self.command_timeout,
                                  context=session.model.context if isinstance(session.model.context, CompactContextManager) else None),
            session=session,
            conn=self.conn,
            loop=loop,
            system_template=SYSTEM_PROMPT,
            instance_template="{{ task }}",
            step_limit=self.step_limit,
            cost_limit=0,
            output_path=session.model.run_dir / "trajectory.traj.json",
        )
        try:
            status = agent.run(task).get("exit_status")
        except (ContextWindowExceeded, ContextCompactionError) as e:
            # Already reported to the editor; end the turn instead of failing the RPC (clients may retry it).
            status = type(e).__name__
        if status == "Submitted":
            session.model.context.remember(agent.messages)
        return status


@app.command()
def main(
    step_limit: int = typer.Option(32, min=1, help="每次任務的模型呼叫上限"),
    command_timeout: int = typer.Option(120, min=1, help="單一工具命令逾時秒數"),
    http_timeout: int = typer.Option(600, min=1, help="單次模型請求逾時秒數"),
    context_window: int | None = typer.Option(None, min=2048, help="harness 強制的上下文視窗（tokens）"),
    context_policy: Literal["compact", "sliding", "append", "memory"] | None = typer.Option(None, help="上下文策略"),
    reasoning: Literal["none", "low", "high"] | None = typer.Option(None, help="推理程度"),
):
    logging.getLogger("minisweagent").handlers = [logging.StreamHandler(sys.stderr)]
    with httpx.Client(trust_env=False, timeout=http_timeout) as http_client:
        agent = WindowsAcpAgent(
            http_client,
            step_limit=step_limit,
            command_timeout=command_timeout,
            model_overrides={
                "context_window": context_window,
                "context_policy": context_policy,
                "reasoning_effort": reasoning,
            },
        )
        try:
            asyncio.run(run_agent(agent))
        finally:
            for session in agent.sessions.values():
                session.model.close()


if __name__ == "__main__":
    app()
