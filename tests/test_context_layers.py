"""Exercise real disk storage and PowerShell; summaries deliberately omit or contradict evidence."""

import json
from pathlib import Path

import httpx
import pytest
from minisweagent.context_compaction import CompactContextManager
from minisweagent.context_store import ContextStore, admit, preview
from minisweagent.environments.powershell import PowerShellEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.models.windows_host import WindowsHostModel
from openai.types.chat import ChatCompletion


def group(index: int, text: str) -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{index}", "type": "function",
            "function": {"name": "powershell", "arguments": json.dumps({"command": f"Read evidence {index}"})}}]},
        {"role": "tool", "tool_call_id": f"c{index}", "content": text},
    ]


def assert_pairs(messages: list[dict]) -> None:
    assert [c["id"] for m in messages for c in m.get("tool_calls", [])] == [
        m["tool_call_id"] for m in messages if m["role"] == "tool"
    ]


def test_sixty_compaction_rounds_keep_corrections_and_retrieve_omitted_evidence(tmp_path: Path):
    summaries = []

    def lossy_summary(prompt, path):
        assert context.count(prompt, []) <= context.input_budget
        assert_pairs(json.loads(prompt[1]["content"])["messages"])
        summaries.append(prompt)
        return "過時摘要：連接埠是 4100。其餘細節未保留。"

    context = CompactContextManager(context_window=8192, max_output_tokens=1024, summarize=lossy_summary)
    try:
        first = [{"role": "system", "content": "固定規則"}, {"role": "user", "content": "目標：完成 R17。最初連接埠 4100。"}]
        context.prepare(first, [], archive=tmp_path)
        original = context.store.ref("event", context._request_id)
        assert "remembered" in context.store.tool({"operation": "remember", "key": "service-port",
            "text": "連接埠 4100", "source": original})
        hidden = context.store.offload("開頭\n" + "雜訊" * 10000 + "\nOLD-EVIDENCE-R17: digest=7f3a91\n" + "雜訊" * 10000)
        context.remember(first + [{"role": "assistant", "content": "開始執行"}])
        for turn in range(60):
            messages = [{"role": "system", "content": "固定規則"}, {"role": "user", "content":
                "更正：服務連接埠改成 5200，4100 已停用。" if turn == 3 else f"繼續 R17 第 {turn} 段"}]
            context.prepare(messages, [], archive=tmp_path)
            if turn == 3:
                correction = context.store.ref("event", context._request_id)
                assert "remembered" in context.store.tool({"operation": "remember", "key": "service-port",
                    "text": "連接埠 5200；4100 已停用", "source": correction})
            for step in range(18):
                messages.extend(group(turn * 100 + step, f"證據 {turn}/{step}: " + "1234567890" * 70))
            prepared = context.prepare(messages, [], archive=tmp_path)
            assert prepared[0] == messages[0] and messages[1] in prepared
            assert context.count(prepared, []) <= context.input_budget
            assert_pairs(prepared)
            memory = json.loads(prepared[1]["content"].split("\n", 1)[1])["notes"]
            assert len(memory) == 1 and ("5200" if turn >= 3 else "4100") in memory[0]["text"]
            context.remember(messages + [{"role": "assistant", "content": f"已完成第 {turn} 段"}])
        assert len(summaries) >= 60 and context.turns == []
        assert len(context.store.history(context._turn, context._cut)) < 60
        assert context.store.db.execute("SELECT count(*) FROM events").fetchone()[0] > 2000
        assert context.store.tool({"operation": "read", "ref": original})["text"].endswith("4100。")
        assert "5200" in context.store.tool({"operation": "read", "ref": correction})["text"]
        hits = context.store.tool({"operation": "search", "query": "OLD-EVIDENCE-R17"})["hits"]
        assert [h["ref"] for h in hits] == [hidden] and "7f3a91" in hits[0]["excerpt"]
        offset, pages = 0, []
        while offset is not None:
            page = context.store.tool({"operation": "read", "ref": hidden, "offset": offset, "limit": 1024})
            pages.append(page["text"])
            offset = page["next_offset"]
        assert "OLD-EVIDENCE-R17: digest=7f3a91" in "".join(pages)
        assert all(len(p) <= 1024 for p in pages)
    finally:
        context.close()


def test_admission_masks_duplicates_preserves_tail_and_never_mutates_raw_history(tmp_path: Path):
    raw = "開頭 HEAD\n" + "測試" * 10000 + "\n尾端 FAIL-E42"
    assert len(preview(raw).encode()) <= 6000
    assert "HEAD" in preview(raw) and "FAIL-E42" in preview(raw)
    messages = []
    for i in range(8):
        messages.extend(group(i, raw))
    original = json.dumps(messages, ensure_ascii=False)
    views = admit(list(enumerate(messages, 1)), keep=2)
    assert_pairs(views)
    assert json.loads(views[1]["content"])["duplicate"]
    assert "FAIL-E42" in views[-1]["content"]
    assert json.dumps(messages, ensure_ascii=False) == original
    context = CompactContextManager(context_window=8192, max_output_tokens=1024,
        summarize=lambda prompt, path: pytest.fail("A single oversized observation should be offloaded, not summarized."))
    try:
        prepared = context.prepare([{"role": "system", "content": "規則"}, {"role": "user", "content": "讀取失敗碼"}]
                                   + group(99, raw), [], archive=tmp_path)
        assert context.count(prepared, []) < context.input_budget
        ref = json.loads(prepared[-1]["content"])["ref"]
        assert context.store.source(ref)[1] == raw and "FAIL-E42" in prepared[-1]["content"]
    finally:
        context.close()


def test_memory_sources_budget_forget_and_new_session_isolation(tmp_path: Path):
    context = CompactContextManager(context_window=8192, max_output_tokens=1024, summarize=lambda p, f: "摘要")
    try:
        messages = [{"role": "system", "content": "規則"}, {"role": "user", "content": "禁止外傳資料"},
                    {"role": "assistant", "content": "未驗證的猜測"}]
        context.prepare(messages, [], archive=tmp_path)
        store = context.store
        user, guess = store.ref("event", 1), store.ref("event", 2)
        assert "error" in store.tool({"operation": "remember", "key": "rule", "text": "不能外傳", "source": guess})
        assert "remembered" in store.tool({"operation": "remember", "key": "rule", "text": "不能外傳", "source": user})
        store.memory_budget = 10
        assert "error" in store.tool({"operation": "remember", "key": "rule", "text": "測" * 100, "source": user})
        assert store.memories()[0]["text"] == "不能外傳"
        assert "error" in store.tool({"operation": "read", "ref": "../../private.env"})
        assert "error" in store.tool({"operation": "read", "offset": "1"})
        assert not store.tool({"operation": "search", "query": "' OR 1=1 --"})["hits"]
        assert store.tool({"operation": "forget", "key": "rule"}) == {"forgotten": "rule"}
        assert not store.memories()
        context.clear()
        context.prepare(messages, [], archive=tmp_path)
        assert "error" in context.store.tool({"operation": "read", "ref": user})
        assert not context.store.memories() and context.store.path != store.path
    finally:
        context.close()


def test_native_context_tool_routing_and_real_powershell_offload(tmp_path: Path):
    with httpx.Client(trust_env=False) as client:
        model = WindowsHostModel(api_key="test-no-network", http_client=client)
        try:
            model.context.prepare([{"role": "system", "content": "規則"}, {"role": "user", "content": "查回中段證據"}],
                                  [], archive=tmp_path)
            environment = PowerShellEnvironment(output_dir=tmp_path / "tools", cwd=str(tmp_path), context=model.context)
            output = environment.execute({"command": "Write-Output ('HEAD' + ('測' * 4000) + 'MIDDLE-EVIDENCE-42' + ('測' * 4000) + 'TAIL')"})
            assert output["truncated"] and "TAIL" in output["output"] and "MIDDLE-EVIDENCE-42" not in output["output"]
            response = ChatCompletion.model_validate({"id": "routing", "object": "chat.completion", "created": 1,
                "model": "unused", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "tool_calls": [{"id": "ctx1", "type": "function", "function": {
                        "name": "context", "arguments": json.dumps({"operation": "search", "query": "MIDDLE-EVIDENCE-42"})}}]}}]})
            action = model.parse_actions(response.choices[0].message.tool_calls)[0]
            assert action["tool_call_id"] == "ctx1" and "context" in action
            found = environment.execute(action)
            assert found["returncode"] == 0
            assert json.loads(found["output"])["hits"][0]["ref"] == output["ref"]
            assert "MIDDLE-EVIDENCE-42" in model.context.store.tool({"operation": "read", "ref": output["ref"], "offset": 3900})["text"]
            response.choices[0].message.tool_calls[0].function.arguments = "{bad json"
            with pytest.raises(FormatError):
                model.parse_actions(response.choices[0].message.tool_calls)
        finally:
            model.close()


def test_disk_notes_and_evidence_can_be_reopened_without_an_llm(tmp_path: Path):
    path = tmp_path / "session.sqlite3"
    store = ContextStore(path)
    store.sync(1, [{"role": "user", "content": "已確認 artifact SHA 為 f93d"}])
    source = store.ref("event", 1)
    store.tool({"operation": "remember", "key": "artifact-sha", "text": "SHA=f93d", "source": source})
    store.close()
    reopened = ContextStore(path)
    try:
        assert reopened.memories() == [{"key": "artifact-sha", "text": "SHA=f93d", "source": source}]
        assert reopened.tool({"operation": "read", "ref": source})["text"] == "已確認 artifact SHA 為 f93d"
    finally:
        reopened.close()


def test_unicode_pagination_survives_actual_context_assembly_without_skipping_bytes(tmp_path: Path):
    context = CompactContextManager(context_window=8192, max_output_tokens=1024, summarize=lambda p, f: "進度摘要")
    try:
        messages = [{"role": "system", "content": "規則"}, {"role": "user", "content": "逐頁讀取所有證據"}]
        context.prepare(messages, [], archive=tmp_path)
        original = "中文證據\\\"\n" * 1600 + "最後的結論 END"
        ref = context.store.offload(original)
        offset, pages, index = 0, [], 0
        while offset is not None:
            result = context.store.tool({"operation": "read", "ref": ref, "offset": offset, "limit": 4000})
            calls = group(index, json.dumps({"output": json.dumps(result, ensure_ascii=False), "returncode": 0}))
            calls[0]["tool_calls"][0]["function"] = {"name": "context", "arguments": json.dumps({"operation": "read", "ref": ref})}
            messages.extend(calls)
            prepared = context.prepare(messages, [], archive=tmp_path)
            replayed = json.loads(json.loads(prepared[-1]["content"])["output"])
            assert replayed == result and len(result["text"]) > 0
            assert len(json.dumps(result, ensure_ascii=False).encode()) <= context.store.read_budget
            assert_pairs(prepared)
            pages.append(replayed["text"])
            offset, index = replayed["next_offset"], index + 1
        assert "".join(pages) == original and index > 10
    finally:
        context.close()
