"""Live debugger/console check using the same entry point as F5."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    debugpy = next(
        (Path(os.environ["USERPROFILE"]) / ".vscode/extensions").glob("ms-python.debugpy-*/bundled/libs/debugpy")
    )
    previous = set((ROOT / ".runs").iterdir()) if (ROOT / ".runs").exists() else set()
    result = subprocess.run(
        [sys.executable, str(debugpy), "--listen", "127.0.0.1:0", "-m", "minisweagent.run.windows_console"],
        input=(
            "請記住本輪測試碼 F5_READY_731。不要執行工具，只回答已記住。\n"
            "/reasoning low\n"
            "剛剛的測試碼是什麼？不要執行工具，只回答測試碼。\n"
            "/exit\n"
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        env=os.environ | {"PYTHONUTF8": "1", "MSWEA_SILENT_STARTUP": "1"},
        timeout=240,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    assert "F5_READY_731" in result.stdout, result.stdout
    session = next(iter(set((ROOT / ".runs").iterdir()) - previous))
    first = json.loads((session / "turn-0001/request-0001.json").read_text(encoding="utf-8"))
    second = json.loads((session / "turn-0002/request-0001.json").read_text(encoding="utf-8"))
    assert first["reasoning_effort"] == "none"
    assert second["reasoning_effort"] == "low"
    assert second["max_completion_tokens"] == 8192
    assert any(
        "F5_READY_731" in (message.get("content") or "") for message in second["messages"] if message["role"] == "user"
    )
    assert all("api_key" not in request for request in (first, second))
    print("PASS debugger entry, UTF-8 console input, conversation memory, none -> low, clean exit")


if __name__ == "__main__":
    main()
