import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from minisweagent.context_compaction import CompactContextManager, ContextCompactionError
from minisweagent.context_manager import ContextWindowExceeded


def tool_group(index: int, text: str = "0" * 180) -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{index}", "type": "function", "function": {"name": "powershell", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": f"c{index}", "content": text},
    ]


def task(context: CompactContextManager, ratio: float) -> list[dict]:
    messages = [{"role": "system", "content": "規則"}, {"role": "user", "content": "保留我的完整任務 R17"}]
    while context.count(messages, []) < context.input_budget * ratio:
        messages.extend(tool_group(len(messages)))
    return messages


def pairs(messages: list[dict]) -> None:
    calls = [call["id"] for message in messages for call in message.get("tool_calls", [])]
    results = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
    assert calls == results


@pytest.fixture
def contexts():
    opened = []

    def create(summarize):
        context = CompactContextManager(context_window=8192, max_output_tokens=1024, summarize=summarize)
        opened.append(context)
        return context

    yield create
    for context in opened:
        context.close()


def test_background_snapshot_keeps_new_messages_and_original_task(contexts, tmp_path: Path):
    started, release = Event(), Event()
    snapshots, events = [], []

    def summarize(prompt, path):
        snapshots.append(json.loads(prompt[1]["content"]))
        started.set()
        assert release.wait(5)
        return "目標 R17；已完成早期工具查詢。"

    context = contexts(summarize)
    context.on_event = events.append
    messages = task(context, 0.73)
    original = json.dumps(messages, ensure_ascii=False)
    try:
        prepared = context.prepare(messages, [], archive=tmp_path)
        assert started.wait(3)
        assert prepared == messages and not context._pending.future.done()
        messages.extend(tool_group(999, "摘要期間新到的證據 NEW-999"))
        release.set()
        context._pending.future.result(timeout=3)
        prepared = context.prepare(messages, [], archive=tmp_path)
        assert "目標 R17" in prepared[0]["content"]
        assert prepared[1] == messages[1] and prepared[-1] == messages[-1]
        assert "NEW-999" not in json.dumps(snapshots)
        assert json.dumps(messages[:-2], ensure_ascii=False) == original
        assert context.count(prepared, []) < context.input_budget
        pairs(prepared)
        checkpoint = json.loads((tmp_path / "context-summary.json").read_text(encoding="utf-8"))
        assert checkpoint["cut"] == context._cut
        assert checkpoint["summary"] == "目標 R17；已完成早期工具查詢。"
        assert len(events) == 2
    finally:
        release.set()


def test_blocking_threshold_waits_and_repeated_summaries_chain(contexts, tmp_path: Path):
    started, release = Event(), Event()
    snapshots, events = [], []

    def summarize(prompt, path):
        snapshots.append(json.loads(prompt[1]["content"]))
        started.set()
        assert release.wait(5)
        return f"摘要 {len(snapshots)}：持續保留 R17 進度。"

    context = contexts(summarize)
    context.on_event = events.append
    messages = task(context, 0.94)
    with ThreadPoolExecutor(max_workers=1) as executor:
        prepared = executor.submit(context.prepare, messages, [], archive=tmp_path)
        try:
            assert started.wait(3) and not prepared.done()
            release.set()
            assert context.count(prepared.result(timeout=3), []) < context.input_budget * 0.7
        finally:
            release.set()
    first_cut = context._cut
    for index in range(30):
        messages.extend(tool_group(1000 + index))
    prepared = context.prepare(messages, [], archive=tmp_path)
    assert context._cut > first_cut
    assert snapshots[1]["previous_summary"] == "摘要 1：持續保留 R17 進度。"
    assert prepared[1] == messages[1] and prepared[-1] == messages[-1]
    assert any("等待摘要" in text for text in events)
    pairs(prepared)


@pytest.mark.parametrize(("reset",), [(True,), (False,)])
def test_reset_or_replacement_discards_stale_summaries(contexts, tmp_path: Path, reset: bool):
    started, release = Event(), Event()

    def summarize(prompt, path):
        started.set()
        assert release.wait(5)
        return "舊任務的機密摘要 R17"

    context = contexts(summarize)
    messages = task(context, 0.73)
    try:
        context.prepare(messages, [], archive=tmp_path)
        assert started.wait(3)
        old_future = context._pending.future
        if reset:
            context.clear()
        fresh = [{"role": "system", "content": "規則"}, {"role": "user", "content": "新任務"}]
        assert context.prepare(fresh, [], archive=tmp_path) == fresh
        release.set()
        old_future.result(timeout=3)
        assert context.prepare(fresh, [], archive=tmp_path) == fresh
        assert not (tmp_path / "context-summary.json").exists()
        context.prepare(task(context, 0.94), [], archive=tmp_path)
        assert context._summary
        assert context.prepare(fresh, [], archive=tmp_path) == fresh
        assert not context._summary
    finally:
        release.set()


def test_remembered_turn_retains_summary_across_new_task(contexts, tmp_path: Path):
    context = contexts(lambda prompt, path: "R17 舊任務已驗證的事實。")
    messages = task(context, 0.94)
    context.prepare(messages, [], archive=tmp_path)
    messages.append({"role": "exit", "content": "完成", "extra": {"exit_status": "Submitted"}})
    context.remember(messages)
    current = [{"role": "system", "content": "規則"}, {"role": "user", "content": "新問題 R18"}]
    prepared = context.prepare(current, [], archive=tmp_path)
    assert "R17 舊任務" in prepared[0]["content"]
    assert prepared[-1] == current[-1]
    assert any(message.get("content") == "完成" for message in prepared)
    pairs(prepared)


def test_failed_background_summary_preserves_history_until_blocking_threshold(contexts, tmp_path: Path):
    def summarize(prompt, path):
        raise ValueError("summary service unavailable")

    events = []
    context = contexts(summarize)
    context.on_event = events.append
    messages = task(context, 0.73)
    original = json.dumps(messages)
    context.prepare(messages, [], archive=tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        context._pending.future.result(timeout=3)
    assert context.prepare(messages, [], archive=tmp_path) == messages
    assert not context._summary and json.dumps(messages) == original
    assert any("失敗" in text for text in events)
    for index in range(20):
        messages.extend(tool_group(1000 + index))
    with pytest.raises(ContextCompactionError, match="摘要失敗"):
        context.prepare(messages, [], archive=tmp_path)
    assert not (tmp_path / "context-summary.json").exists()


def test_wait_is_cancellable_and_latest_oversized_input_is_never_dropped(contexts, tmp_path: Path):
    started, release, cancel = Event(), Event(), Event()

    def summarize(prompt, path):
        started.set()
        assert release.wait(5)
        return "摘要"

    def check_cancelled():
        if cancel.is_set():
            raise InterruptedError("cancelled")

    context = contexts(summarize)
    context.check_cancelled = check_cancelled
    with ThreadPoolExecutor(max_workers=1) as executor:
        preparing = executor.submit(context.prepare, task(context, 0.94), [], archive=tmp_path)
        try:
            assert started.wait(3)
            cancel.set()
            with pytest.raises(InterruptedError, match="cancelled"):
                preparing.result(timeout=3)
        finally:
            release.set()
    cancel.clear()
    context.clear()
    messages = [{"role": "system", "content": "規則"}, {"role": "user", "content": "0" * 10000}]
    with pytest.raises(ContextWindowExceeded, match="沒有可安全壓縮"):
        context.prepare(messages, [], archive=tmp_path)
    assert len(messages[-1]["content"]) == 10000


def test_overfull_history_compacts_in_bounded_chunks_and_keeps_multi_tool_group(contexts, tmp_path: Path):
    snapshots = []

    def summarize(prompt, path):
        assert context.count(prompt, []) <= context.input_budget
        snapshots.append(json.loads(prompt[1]["content"]))
        pairs(snapshots[-1]["messages"])
        return "累積已驗證的 R17 進度。"

    context = contexts(summarize)
    messages = task(context, 3.5)
    latest = tool_group(9991, "最新證據 A")
    latest[0]["tool_calls"] += tool_group(9992)[0]["tool_calls"]
    latest += [{"role": "tool", "tool_call_id": "c9992", "content": "最新證據 B"}]
    messages.extend(latest)
    prepared = context.prepare(messages, [], archive=tmp_path)
    assert len(snapshots) > 1 and snapshots[1]["previous_summary"]
    assert context.count(prepared, []) < context.input_budget
    assert prepared[-3:] == latest
    assert prepared[1] == messages[1]
    pairs(prepared)
