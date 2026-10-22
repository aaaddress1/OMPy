import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from minisweagent.context_manager import ContextManager
from minisweagent.models.windows_host import WindowsHostConfig
from minisweagent.run.console_ui import COMMANDS, ConsoleUI, input_session, terminal_text
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console


@pytest.mark.parametrize(("width",), [(42,), (100,)])
def test_console_renders_unicode_stream_tool_failure_and_expands_archived_output(tmp_path: Path, width: int):
    output = StringIO()
    model = SimpleNamespace(config=WindowsHostConfig(model_name="local-model"), context=ContextManager())
    ui = ConsoleUI(model, tmp_path, tmp_path / "session", console=Console(file=output, width=width))
    ui.welcome()
    ui.user("多行任務\n請保留 [原文]，不能解讀為標籤")
    with ui.responding():
        ui.delta("reasoning_content", "先確認原始資料。")
        ui.delta("content", "**整理完成**\n\n```python\nprint('中文')\n```")
    archive = tmp_path / "tool.json"
    archive.write_text(json.dumps({"output": "完整中段資料\n" * 12 + "ARCHIVE-END"}), encoding="utf-8")
    with ui.running_tool({"tool_call_id": "c1", "command": "Get-Item missing.txt"}):
        pass
    ui.tool_result("c1", {"output": "錯誤\n" * 10, "returncode": 1, "archive": str(archive), "truncated": True})
    ui.command("/last")
    ui.command("/help")
    assert all(text in output.getvalue() for text in ("Oh My PiPy", "[原文]", "整理完成", "ARCHIVE-END", "exit 1", "Alt+Enter"))
    assert "\x1b[" not in output.getvalue()
    ui.command("/thoughts")
    assert not ui.thoughts
    ui.command("/tools")
    assert ui.expanded
    ui.usage({"extra": {"response": {"usage": {"prompt_tokens": 1234, "completion_tokens": 89}}}})
    assert "1.2k" in ui.status() and ui.calls == 1


@pytest.mark.parametrize(("keys", "expected"), [
    ("第一行\x1b\r第二行\r", "第一行\n第二行"),
    ("\x1b[200~貼上第一行\n貼上第二行\x1b[201~\r", "貼上第一行\n貼上第二行"),
    ("/hel\t\r", "/help"),
])
def test_real_terminal_editor_multiline_paste_and_slash_completion(keys: str, expected: str):
    with create_pipe_input() as pipe, ThreadPoolExecutor(max_workers=1) as executor:
        editor = input_session(input=pipe, output=DummyOutput())
        result = executor.submit(editor.prompt, "> ")
        pipe.send_text(keys)
        assert result.result(timeout=5) == expected


async def wait_for_editor(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize(("width",), [(42,), (100,)])
def test_slash_menu_renders_without_tab_and_enter_confirms_before_submitting(width: int):
    async def run():
        output = StringIO()
        with create_pipe_input() as pipe:
            editor = input_session(input=pipe, output=Vt100_Output(output, lambda: Size(rows=24, columns=width),
                term="xterm-256color", enable_cpr=False))
            result = asyncio.create_task(editor.prompt_async("> "))
            try:
                pipe.send_text("/")
                await wait_for_editor(lambda: all(command in output.getvalue() for command in COMMANDS))
                assert "顯示快捷鍵" in output.getvalue()
                state = editor.default_buffer.complete_state
                assert state and len(state.completions) == len(COMMANDS) and state.complete_index is None
                pipe.send_text("\x1b[B\x1b[B")
                await wait_for_editor(lambda: editor.default_buffer.text == "/new")
                pipe.send_text("\x1b[A\r")
                await wait_for_editor(lambda: editor.default_buffer.text == "/help"
                    and editor.default_buffer.complete_state is None)
                assert not result.done()
                pipe.send_text("\r")
                assert await asyncio.wait_for(result, 5) == "/help"
            finally:
                result.cancel()
                await asyncio.gather(result, return_exceptions=True)
    asyncio.run(run())


def test_slash_menu_filters_arguments_dismisses_and_preserves_normal_input_and_history():
    async def run():
        with create_pipe_input() as pipe:
            editor = input_session(input=pipe, output=DummyOutput())
            result = asyncio.create_task(editor.prompt_async("> "))
            try:
                pipe.send_text("/re")
                await wait_for_editor(lambda: editor.default_buffer.complete_state
                    and len(editor.default_buffer.complete_state.completions) == 1)
                pipe.send_text("\r")
                await wait_for_editor(lambda: editor.default_buffer.text == "/reasoning "
                    and editor.default_buffer.complete_state
                    and len(editor.default_buffer.complete_state.completions) == 3)
                pipe.send_text("h")
                await wait_for_editor(lambda: editor.default_buffer.complete_state
                    and [c.text for c in editor.default_buffer.complete_state.completions] == ["/reasoning high"])
                pipe.send_text("\r")
                await wait_for_editor(lambda: editor.default_buffer.text == "/reasoning high")
                assert not result.done()
                pipe.send_text("\r")
                assert await asyncio.wait_for(result, 5) == "/reasoning high"

                result = asyncio.create_task(editor.prompt_async("> "))
                pipe.send_text("/")
                await wait_for_editor(lambda: editor.default_buffer.complete_state)
                pipe.send_text("\x1b")
                await wait_for_editor(lambda: editor.default_buffer.complete_state is None)
                assert editor.default_buffer.text == "/" and not result.done()
                pipe.send_text("\x15解釋 /he")
                await wait_for_editor(lambda: editor.default_buffer.text == "解釋 /he")
                await asyncio.sleep(0.1)
                assert editor.default_buffer.complete_state is None
                pipe.send_text("\r")
                assert await asyncio.wait_for(result, 5) == "解釋 /he"

                result = asyncio.create_task(editor.prompt_async("> "))
                pipe.send_text("\x1b[A\r")
                assert await asyncio.wait_for(result, 5) == "解釋 /he"
            finally:
                result.cancel()
                await asyncio.gather(result, return_exceptions=True)
    asyncio.run(run())


def test_core_engine_is_general_purpose_and_has_no_experiment_imports():
    root = Path(__file__).parents[1]
    source = root / "mini-swe-agent/src/minisweagent"
    files = [source / name for name in ("context_store.py", "context_compaction.py", "models/windows_host.py",
             "environments/powershell.py", "run/windows_console.py", "run/windows_acp.py", "run/console_ui.py")]
    for file in files:
        text = file.read_text(encoding="utf-8").lower()
        assert not any(term in text for term in ("forensic", "鑑識", "取證", "鑒識", "experiments/")), file.name


def test_console_commands_work_in_a_real_process_without_contacting_any_model(tmp_path: Path):
    root = Path(__file__).parents[1]
    process = subprocess.run([sys.executable, "-m", "minisweagent.run.windows_console", "--plain", "--cwd", str(tmp_path)],
        input="/help\n/thoughts\n/tools\n/new\n/reasoning\n/reasoning high\n/unknown\n/exit\n",
        text=True, capture_output=True, encoding="utf-8", timeout=30,
        env=os.environ | {"PYTHONPATH": str(root / "mini-swe-agent/src"), "HARNESS_API_KEY": "test-no-network",
                          "HARNESS_BASE_URL": "http://127.0.0.1:1/v1", "HARNESS_MODEL": "test-model"})
    assert process.returncode == 0, process.stderr
    assert all(text in process.stdout for text in ("Oh My PiPy", "快捷鍵", "已開始新對話", "可使用", "high", "未知指令"))
    assert "\x1b[" not in process.stdout


def test_live_stream_updates_and_terminal_control_sequences_are_not_replayed():
    output = StringIO()
    model = SimpleNamespace(config=WindowsHostConfig(model_name="local-model"), context=ContextManager())
    ui = ConsoleUI(model, Path("workspace"), Path("session"), console=Console(file=output, width=80, force_terminal=True,
        legacy_windows=False, _environ={"TERM": "xterm-256color"}))
    with ui.responding():
        ui.delta("reasoning_content", "這是模型實際回傳欄位的呈現測試")
        ui.delta("content", "回應已經逐步出現")
        ui._live.refresh()
        assert "回應已經逐步出現" in output.getvalue()
    assert ui._live is None
    assert terminal_text("正常\x1b[2J文字\x1b]52;c;ZmFrZQ==\x07\n") == "正常文字\n"
