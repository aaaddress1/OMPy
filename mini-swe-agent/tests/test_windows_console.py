import ctypes
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from minisweagent.context_manager import ContextManager, ContextWindowExceeded, estimate_tokens
from minisweagent.environments.powershell import PowerShellEnvironment


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")
@pytest.mark.parametrize(("shell_name",), [("powershell.exe",), ("pwsh.exe",)])
def test_invalid_commands_are_plain_text_and_standard_commands_work(tmp_path: Path, shell_name: str):
    if not shutil.which(shell_name):
        pytest.skip(f"{shell_name} is not installed")
    (tmp_path / "證據.txt").write_text("evidence", encoding="utf-8")
    env = PowerShellEnvironment(output_dir=tmp_path / "outputs", cwd=str(tmp_path), shell=shell_name)
    for command in (
        "Get-PowerShellVersion 2>$null; $PSVersionTable",
        "Get-ChildItem . | Select-FirstN 10",
        "Get-ChildItem . | Select-First 10",
    ):
        failed = env.execute({"command": command})
        archive = json.loads(Path(failed["archive"]).read_text(encoding="utf-8"))
        assert failed["returncode"] != 0
        assert "not recognized" in failed["output"]
        assert "#< CLIXML" not in failed["output"] and "_x000D_" not in failed["output"]
        assert archive["output"] == failed["output"]
        assert Path(archive["script"]).read_text(encoding="utf-8-sig").find(command) >= 0
    valid = env.execute({"command": "Get-ChildItem -File -Name | Select-Object -First 1"})
    assert valid["returncode"] == 0 and valid["output"].strip() == "證據.txt"
    version = env.execute({"command": "$PSVersionTable.PSVersion.ToString()"})
    assert version["returncode"] == 0
    assert version["output"].strip().startswith("5.1." if shell_name == "powershell.exe" else "7.")


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")
def test_powershell_unicode_archive_and_native_exit_code(tmp_path: Path):
    env = PowerShellEnvironment(output_dir=tmp_path / "outputs", cwd=str(tmp_path))
    output = env.execute({"command": "Write-Output ('證據 ' + ('測' * 3000)); exit 7"})
    archive = json.loads(Path(output["archive"]).read_text(encoding="utf-8"))
    assert output["returncode"] == 7
    assert output["truncated"]
    assert len(output["output"].encode("utf-8")) <= 6000
    assert archive["output"].strip() == "證據 " + "測" * 3000
    assert archive["returncode"] == 7
    command = f"& '{sys.executable.replace(chr(39), chr(39) * 2)}' -c 'import sys; print(17 * 19); sys.exit(9)'"
    native = env.execute({"command": command})
    assert native["returncode"] == 9
    assert native["output"].strip() == "323"


@pytest.mark.skipif(os.name != "nt", reason="Windows process tree cleanup")
def test_powershell_timeout_kills_native_child(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "child.py"
    script.write_text(
        f"import os, time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    output = PowerShellEnvironment(output_dir=tmp_path / "outputs", cwd=str(tmp_path), timeout=10).execute(
        {"command": f"& '{sys.executable}' '{script}'"}
    )
    assert output["returncode"] == -1
    assert output["extra"]["exception_type"] == "TimeoutExpired"
    pid = int(pid_file.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)


def test_context_budget_preserves_tool_pairs_and_original_history(tmp_path: Path):
    context = ContextManager(context_window=4096, max_output_tokens=1024)
    context.remember([{"role": "system", "content": "規則"}, {"role": "user", "content": "舊資料" * 1000}])
    messages = [
        {"role": "system", "content": "規則"},
        {"role": "user", "content": "當前任務"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "old", "type": "function", "function": {"name": "powershell", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "old", "content": "0" * 1900},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "new", "type": "function", "function": {"name": "powershell", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "new", "content": "b" * 1000},
    ]
    prepared = context.prepare(messages, [], archive=tmp_path)
    assert context.count(prepared, []) <= context.input_budget
    assert [message["tool_call_id"] for message in prepared if message["role"] == "tool"] == ["new"]
    assert [call["id"] for message in prepared for call in message.get("tool_calls", [])] == ["new"]
    assert prepared[1]["content"] == "當前任務"
    assert len(messages) == 6
    assert messages[0]["content"] == "規則"
    assert "舊資料" in context.turns[0][0]["content"]


def _tool_turns(n: int, content: str) -> list[dict]:
    messages = [{"role": "system", "content": "規則"}, {"role": "user", "content": "當前任務"}]
    for i in range(n):
        messages += [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "powershell", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": f"c{i}", "content": content},
        ]
    return messages


def test_context_budget_trims_again_when_archive_note_tips_over(tmp_path: Path):
    context = ContextManager(context_window=8192, max_output_tokens=1024)
    messages = _tool_turns(4, "x" * 3500)
    messages[-1]["content"] += "y" * round((context.input_budget - context.count(messages[:2] + messages[-4:], [])) * 3.5)
    prepared = context.prepare(messages, [], archive=tmp_path)
    assert context.count(prepared, []) <= context.input_budget
    assert str(tmp_path) in prepared[0]["content"]
    assert [message["tool_call_id"] for message in prepared if message["role"] == "tool"] == ["c3"]


def test_append_policy_overflows_instead_of_trimming(tmp_path: Path):
    sliding = ContextManager(context_window=4096, max_output_tokens=1024)
    append = ContextManager(context_window=4096, max_output_tokens=1024, policy="append")
    messages = _tool_turns(4, "0" * 800)
    assert [m["tool_call_id"] for m in sliding.prepare(messages, [], archive=tmp_path) if m["role"] == "tool"] == [
        "c2",
        "c3",
    ]
    with pytest.raises(ContextWindowExceeded, match="爆窗口"):
        append.prepare(messages, [], archive=tmp_path)
    assert len(append.prepare(messages[:4], [], archive=tmp_path)) == 4


def test_memory_policy_injects_notes_while_trimming(tmp_path: Path):
    context = ContextManager(context_window=4096, max_output_tokens=1024, policy="memory")
    prepared = context.prepare(_tool_turns(4, "0" * 800), [], archive=tmp_path, notes=["規則 R1：TPE 主機是 UTC+8"])
    assert "規則 R1：TPE 主機是 UTC+8" in prepared[0]["content"]
    assert context.count(prepared, []) <= context.input_budget
    assert [m["tool_call_id"] for m in prepared if m["role"] == "tool"] == ["c2", "c3"]
    assert "R1" not in ContextManager(policy="sliding").prepare(_tool_turns(1, "x"), [], archive=tmp_path, notes=["R1"])[0][
        "content"
    ]


def test_token_estimate_is_calibrated_by_server_usage(tmp_path: Path):
    context = ContextManager(context_window=32768, max_output_tokens=8192)
    messages = _tool_turns(1, "鑑識" * 100 + "0101" * 100 + "Get-ChildItem " * 50)
    raw = context.count(context.prepare(messages, [], archive=tmp_path), [])
    context.observe(raw * 2)
    assert context.scale == pytest.approx(2.0, rel=1e-2)
    assert context.count(messages, []) == pytest.approx(raw * 2, abs=2)
    assert estimate_tokens("0101") == 4
    assert estimate_tokens("鑑識") == 2
    assert estimate_tokens("abcdefg") == pytest.approx(2.0)
