"""Offline replay for human/model review: no API calls, deliberately stale summaries."""

import argparse
import json
from pathlib import Path

from minisweagent.context_compaction import CompactContextManager
from minisweagent.run.windows_console import SYSTEM_PROMPT


def replay(root: Path) -> dict:
    summaries = []

    def summarize(prompt, path):
        summaries.append(prompt)
        return "歷史摘要（故意過時）：taskboard 使用 4100；測試已通過。build digest 已遺漏，須查原始證據。"

    context = CompactContextManager(context_window=8192, max_output_tokens=1024, summarize=summarize)
    try:
        initial = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content":
            "部署 taskboard，只能改 staging，不能修改 production。原先連接埠 4100。最後要回報 build digest。"}]
        context.prepare(initial, [], archive=root)
        source = context.store.ref("event", context._request_id)
        context.store.tool({"operation": "remember", "key": "constraint", "text": "只改 staging；禁止修改 production", "source": source})
        context.store.offload("BUILD-R17/taskboard；digest 位於字元 5000 之後。\n" + "x" * 5000 + "\nbuild_digest=ab91f03d27e84c5a\n")
        context.remember(initial + [{"role": "assistant", "content": "開始工作"}])
        for turn in range(3):
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content":
                "更正：連接埠為 5200；測試尚未執行，不要宣稱通過。" if turn == 0 else "繼續檢查 staging"}]
            context.prepare(messages, [], archive=root)
            if turn == 0:
                source = context.store.ref("event", context._request_id)
                context.store.tool({"operation": "remember", "key": "port", "text": "5200；4100 已停用", "source": source})
                context.store.tool({"operation": "remember", "key": "validation", "text": "測試尚未執行", "source": source})
            for index in range(24):
                call = f"c{turn}-{index}"
                messages += [{"role": "assistant", "content": f"檢查 {call}", "tool_calls": [{"id": call,
                    "type": "function", "function": {"name": "powershell", "arguments": '{"command":"Get-Date"}'}}]},
                    {"role": "tool", "tool_call_id": call, "content": f"合成觀察 {call}: " + "1234567890" * 80}]
            context.prepare(messages, [], archive=root)
            context.remember(messages + [{"role": "assistant", "content": "這段檢查完成"}])
        final = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content":
            "回報目前連接埠、可修改的環境、測試狀態、BUILD-R17 的完整 build digest。缺少的資訊先回查原文。"}]
        prepared = context.prepare(final, [], archive=root)
        (root / "final-context.json").write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {"api_calls": 0, "summary_rounds": len(summaries), "database": str(context.store.path),
                  "context": str(root / "final-context.json"), "input_tokens_estimate": context.count(prepared, [])}
        (root / "replay.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runs/context-review"))
    print(json.dumps(replay(parser.parse_args().output.resolve())), flush=True)
