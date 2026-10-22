"""Render a synthetic console preview without loading credentials or contacting a model."""

import argparse
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from minisweagent.context_manager import ContextManager
from minisweagent.models.windows_host import WindowsHostConfig
from minisweagent.run.console_ui import ACCENT, MUTED, ConsoleUI
from rich.console import Console
from rich.panel import Panel
from rich.text import Text


def preview(output: Path, width: int = 100) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    console = Console(file=StringIO(), width=width, record=True, force_terminal=True)
    ui = ConsoleUI(SimpleNamespace(config=WindowsHostConfig(model_name="your-agent-model", reasoning_effort="low"),
        context=ContextManager(context_window=32768, max_output_tokens=4096)),
        Path("workspace/OMPy"), Path(".runs/demo"), console=console)
    ui.interactive = False
    ui.welcome()
    ui.notice("介面示範 · 合成內容 · 沒有 API 呼叫")
    ui.user("整理這份資料的重點，並列出需要再確認的地方。")
    ui.actions["demo"] = {"command": "Get-ChildItem -File | Select-Object Name,Length"}
    ui.tool_result("demo", {"output": "Name             Length\nnotes.md           2048\nrecords.csv        6144", "returncode": 0})
    ui.answer("已整理成 **三項重點**，並保留原始來源。\n\n- 資料範圍與欄位\n- 目前可確認的結果\n- 尚需查證的部分\n\n```python\nsummary = analyze(records)\n```\n\n這是介面示範內容。")
    ui.console.print(Panel(Text("❯ 接下來想完成什麼？\n\nEnter 送出 · Alt+Enter 換行 · Tab 補全"),
                           title="OMPy", title_align="left", border_style=ACCENT))
    ui.console.print(Text(ui.status(), style=MUTED))
    console.save_svg(str(output.with_suffix(".svg")), title="Oh My PiPy · Console", clear=False)
    console.save_html(str(output.with_suffix(".html")), clear=False)
    console.save_text(str(output.with_suffix(".txt")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/console-preview"))
    parser.add_argument("--width", type=int, default=100)
    args = parser.parse_args()
    preview(args.output, args.width)
    print(args.output.with_suffix(".svg").resolve())
