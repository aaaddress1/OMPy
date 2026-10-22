"""Terminal presentation only: Rich transcript, streaming cards and a local input editor."""

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text

COMMANDS = {"/help": "顯示快捷鍵", "/new": "開始新對話", "/exit": "離開", "/reasoning": "none | low | high",
            "/thoughts": "顯示或收起模型思考", "/tools": "切換工具預覽長度", "/last": "展開上一個工具結果"}
ACCENT, MUTED, BORDER = "#5eead4", "#94a3b8", "#334155"


def terminal_text(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", text)
    return "".join(c for c in Text.from_ansi(text).plain if (c >= " " and not "\x7f" <= c <= "\x9f") or c in "\n\t")


def compact_number(value: int) -> str:
    return f"{value / 1_000_000:.1f}M" if value >= 1_000_000 else f"{value / 1000:.1f}k" if value >= 1000 else str(value)


class CommandCompleter(Completer):
    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterator[Completion]:
        text = document.text
        if not text.startswith("/") or "\n" in text or document.cursor_position != len(text):
            return
        choices = ({f"/reasoning {level}": label for level, label in
                    (("none", "停用推理"), ("low", "低程度推理"), ("high", "高程度推理"))}
                   if text.startswith("/reasoning ") else COMMANDS)
        for command, description in choices.items():
            if command.startswith(text):
                yield Completion(command, start_position=-len(text), display_meta=description)


def input_session(**kwargs) -> PromptSession:
    bindings = KeyBindings()
    completer = CommandCompleter()

    @bindings.add("enter")
    def submit(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        if state and state.current_completion:
            buffer.complete_state = None
        elif state and state.completions and buffer.text not in COMMANDS:
            buffer.apply_completion(state.completions[0])
        elif buffer.text != "/reasoning":
            buffer.validate_and_handle()
            return
        if buffer.text == "/reasoning":
            buffer.insert_text(" ")

    @bindings.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @bindings.add("tab")
    def complete(event):
        buffer = event.current_buffer
        matches = list(completer.get_completions(buffer.document, CompleteEvent(completion_requested=True)))
        if buffer.complete_state and buffer.complete_state.current_completion:
            buffer.complete_next()
        elif len(matches) == 1:
            buffer.apply_completion(matches[0])
        elif matches:
            if buffer.complete_state:
                buffer.complete_next()
            else:
                buffer.start_completion(select_first=True)

    @bindings.add("escape", filter=has_completions)
    def dismiss(event):
        event.current_buffer.cancel_completion()

    return PromptSession(multiline=True, key_bindings=bindings, history=InMemoryHistory(),
        completer=completer, complete_while_typing=True,
        enable_history_search=False, prompt_continuation="   · ", reserve_space_for_menu=len(COMMANDS) + 1,
        style=Style.from_dict({"frame": "#334155", "brand": "#5eead4 bold", "hint": "#94a3b8",
                              "completion-menu.completion": "bg:#17212f #e2e8f0",
                              "completion-menu.completion.current": "bg:#115e59 #ffffff bold",
                              "completion-menu.meta.completion": "bg:#17212f #94a3b8",
                              "completion-menu.meta.completion.current": "bg:#115e59 #ffffff",
                              "bottom-toolbar": "bg:#17212f #94a3b8"}), **kwargs)


class ConsoleUI:
    def __init__(self, model, cwd: Path, session_dir: Path, *, console: Console | None = None):
        self.console = console or Console(highlight=False)
        self.model, self.cwd, self.session_dir = model, cwd, session_dir
        self.interactive = self.console.is_terminal and not self.console.is_dumb_terminal
        self.thoughts, self.expanded = True, False
        self._live, self._editor = None, None
        self.reply, self.reasoning, self.activity = "", "", "等候模型回應"
        self.calls, self.elapsed, self.input_tokens, self.output_tokens = 0, 0.0, 0, 0
        self.last_output, self.actions = None, {}

    def header(self):
        title = Text.assemble(("◈  Oh My PiPy", f"bold {ACCENT}"), ("  OMPy", MUTED))
        details = Text.assemble(("通用工作助理", "white"), ("  ·  ", MUTED),
                                (getattr(self.model, "provider_label", "API") + " · " + self.model.config.model_name, ACCENT),
                                ("\n" + str(self.cwd), MUTED))
        return Panel(Group(title, Text(""), details), border_style=BORDER, padding=(1, 2), box=box.ROUNDED)

    def status(self) -> str:
        budget = self.model.context.input_budget
        ratio = min(1.0, self.input_tokens / budget) if budget else 0.0
        bar = "━" * round(ratio * 10) + "─" * (10 - round(ratio * 10))
        member = (f" · 會員 {self.model.member_calls}/{self.model.request_limit} requests"
                  if hasattr(self.model, "member_calls") else "")
        return (f"{bar}  {compact_number(self.input_tokens)} / {compact_number(budget)} context"
                f"  ·  ↓ {compact_number(self.output_tokens)}  ·  {self.calls} calls"
                f"  ·  reasoning {self.model.config.reasoning_effort}  ·  {self.elapsed:.1f}s" + member)

    def welcome(self) -> None:
        self.console.print(self.header())
        self.console.print(Text("  輸入任務即可開始  ·  / 即時指令選單  ·  /help 快捷鍵", style=MUTED))
        self.console.print()

    def prompt(self) -> str:
        if not self.interactive:
            return self.console.input("你> ").strip()
        if self._editor is None:
            self._editor = input_session()
        return self._editor.prompt([("class:frame", "╭─ "), ("class:brand", "OMPy"),
            ("class:hint", "  想完成什麼？\n"), ("class:frame", "╰─ "), ("class:brand", "❯ ")],
            bottom_toolbar=lambda: self.status(),
            rprompt=[("class:hint", "/ 選單 · Enter 確認／送出 · Alt+Enter 換行")]).strip()

    def user(self, text: str) -> None:
        self.console.print(Panel(Text(terminal_text(text)), title="你", title_align="left", border_style=BORDER, padding=(0, 1)))

    def notice(self, text: str, *, error: bool = False) -> None:
        if self._live:
            self.activity = text
            self._live.update(self.stream_view())
        else:
            self.console.print(Text(("! " if error else "· ") + text, style="red" if error else MUTED))

    def stream_view(self):
        blocks = []
        if self.reasoning:
            blocks.append(Panel(Text(terminal_text(self.reasoning[-5000:])), title="思考" if self.thoughts else "思考已收起",
                border_style=BORDER, padding=(0, 1)) if self.thoughts else Text("  思考已收起", style=MUTED))
        if self.reply:
            blocks.append(Markdown(terminal_text(self.reply), code_theme="monokai"))
        blocks.append(Text(""))
        blocks.append(Spinner("dots", text=Text(self.activity, style=MUTED), style=ACCENT))
        return Group(*blocks)

    def delta(self, field: str, text: str) -> None:
        if field == "reasoning_content":
            self.reasoning += text
            self.activity = "模型正在回傳思考內容"
        else:
            self.reply += text
            self.activity = "正在回覆"
        if self._live:
            self._live.update(self.stream_view())

    @contextmanager
    def responding(self):
        self.reply, self.reasoning, self.activity = "", "", "等候模型回應"
        started = time.monotonic()
        with (Live(self.stream_view(), console=self.console, refresh_per_second=12, transient=True,
                   vertical_overflow="ellipsis") if self.interactive else nullcontext(None)) as live:
            self._live = live if self.interactive else None
            try:
                yield
            finally:
                self._live = None
                self.elapsed = time.monotonic() - started
        if self.reasoning:
            if self.thoughts:
                self.console.print(Panel(Text(terminal_text(self.reasoning[-5000:])), title="思考", border_style=BORDER))
        if self.reply:
            self.answer(self.reply)

    def answer(self, text: str) -> None:
        self.console.print(Rule(Text("OMPy", style=f"bold {ACCENT}"), align="left", style=BORDER))
        self.console.print(Markdown(terminal_text(text), code_theme="monokai"))
        self.console.print()

    @contextmanager
    def running_tool(self, action: dict):
        self.actions[action["tool_call_id"]] = action
        label = self.tool_label(action)
        with (Live(Panel(Spinner("dots", text=Text(label), style=ACCENT), border_style=BORDER),
                   console=self.console, refresh_per_second=8, transient=True) if self.interactive else nullcontext()):
            yield

    @staticmethod
    def tool_label(action: dict) -> str:
        return {"search": "搜尋歷史內容", "read": "讀取原始內容", "remember": "更新記憶筆記", "forget": "移除過時筆記"}.get(
            action.get("context", {}).get("operation"), "PowerShell")

    def tool_result(self, call_id: str, output: dict) -> None:
        action = self.actions.pop(call_id, {"command": ""})
        self.last_output = (action, output)
        self.console.print(self.tool_card(action, output))

    def tool_card(self, action: dict, output: dict, *, expanded: bool | None = None):
        expanded = self.expanded if expanded is None else expanded
        lines = terminal_text(output["output"] + ("\n" + output["exception_info"] if output.get("exception_info") else "")).splitlines()
        limit = len(lines) if expanded else 6
        blocks = [Syntax(terminal_text(action["command"]), "json" if "context" in action else "powershell",
                         theme="monokai", word_wrap=True, background_color="default")]
        if lines:
            blocks.append(Text(""))
            blocks.extend(Text(line, no_wrap=not expanded, overflow="ellipsis") for line in lines[:limit])
        if len(lines) > limit or (not expanded and any(Text(line).cell_len > self.console.width - 6 for line in lines)):
            blocks.append(Text("輸出已收起 · /last 展開", style=MUTED))
        if output.get("truncated"):
            blocks.append(Text("原始輸出已保存，可用 context 回查", style=MUTED))
        color = ACCENT if output["returncode"] == 0 else "#fb7185"
        return Panel(Group(*blocks), title=self.tool_label(action), title_align="left", border_style=color,
                     subtitle=f"exit {output['returncode']}", subtitle_align="right", padding=(0, 1))

    def usage(self, message: dict) -> None:
        response = message.get("extra", {}).get("response", {})
        usage = response.get("usage") or {}
        if response:
            self.calls += 1
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens)
            self.output_tokens = usage.get("completion_tokens", 0)

    def command(self, text: str) -> bool:
        if text == "/help":
            self.console.print(Panel(Text("\n".join(f"{key:12} {value}" for key, value in COMMANDS.items())
                + "\n\n/ 開啟選單 · ↑↓ 選擇 · Enter 補入，再按送出 · Esc 收起"
                + "\nAlt+Enter 換行 · Tab 補全 · 選單外 ↑↓ 歷史 · Ctrl+C 中止 · Ctrl+D 離開"),
                title="快捷鍵", border_style=BORDER))
        elif text == "/thoughts":
            self.thoughts = not self.thoughts
            self.notice("思考內容已展開" if self.thoughts else "思考內容已收起")
        elif text == "/tools":
            self.expanded = not self.expanded
            self.notice("工具結果將完整顯示" if self.expanded else "工具結果將顯示六行預覽")
        elif text == "/last":
            if self.last_output:
                action, output = self.last_output
                archive = Path(output.get("archive", ""))
                if archive.suffix == ".json" and archive.is_file():
                    output = output | {"output": json.loads(archive.read_text(encoding="utf-8"))["output"], "truncated": False}
                self.console.print(self.tool_card(action, output, expanded=True))
            else:
                self.notice("目前沒有工具結果")
        else:
            return False
        return True
