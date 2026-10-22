"""Session-local evidence and keyed notes; only bounded views enter the model window."""

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

from minisweagent.context_manager import estimate_tokens

CONTEXT_TOOL = {
    "type": "function", "function": {
        "name": "context",
        "description": (
            "搜尋本次對話的原始證據或分段回讀 ref。記憶遺漏時先 search，再 read，不要猜。"
            "remember 以 key 更新筆記，必須附 user/tool 的 source ref；更正時用同一 key。"
            "筆記是助理整理的資料，附來源不代表內容已驗證。forget 移除過時筆記。"
        ),
        "parameters": {
            "type": "object", "properties": {
                "operation": {"type": "string", "enum": ["search", "read", "remember", "forget"]},
                "query": {"type": "string"}, "ref": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 4000},
                "key": {"type": "string"}, "text": {"type": "string"}, "source": {"type": "string"},
            }, "required": ["operation"], "additionalProperties": False,
        },
    },
}


def digest(message: dict) -> str:
    return sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def preview(text: str, budget: int = 6000) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    marker = "\n[…中段已外存，使用 context read 回讀…]\n"
    half = max(0, (budget - len(marker.encode("utf-8"))) // 2)
    return raw[:half].decode("utf-8", errors="ignore") + marker + raw[-half:].decode("utf-8", errors="ignore")


class ContextStore:
    def __init__(self, path: Path, *, memory_budget: int = 2048, read_budget: int = 5000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.memory_budget = path, memory_budget
        self.read_budget = read_budget
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, turn INTEGER, position INTEGER, hash TEXT,
                role TEXT, content TEXT, message TEXT, committed INTEGER DEFAULT 0,
                UNIQUE(turn, position));
            CREATE TABLE IF NOT EXISTS outputs (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, text TEXT, source TEXT);
        """)

    def close(self) -> None:
        self.db.close()

    def matches(self, turn: int, messages: list[dict]) -> bool:
        stored = self.db.execute("SELECT hash FROM events WHERE turn=? ORDER BY position", (turn,)).fetchall()
        return len(stored) <= len(messages) and all(row[0] == digest(m) for row, m in zip(stored, messages))

    def sync(self, turn: int, messages: list[dict]) -> None:
        count = self.db.execute("SELECT count(*) FROM events WHERE turn=?", (turn,)).fetchone()[0]
        calls = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls", [])}
        with self.db:
            self.db.executemany("INSERT INTO events(turn,position,hash,role,content,message) VALUES(?,?,?,?,?,?)", [
                (turn, i, digest(m), "context" if calls.get(m.get("tool_call_id")) == "context" else m["role"],
                 m.get("content") or "", json.dumps(m, ensure_ascii=False)) for i, m in enumerate(messages[count:], count)
            ])

    def commit(self, turn: int) -> None:
        with self.db:
            self.db.execute("UPDATE events SET committed=1 WHERE turn=?", (turn,))

    def history(self, turn: int, after: int = 0) -> list[tuple[int, dict]]:
        return [(i, json.loads(m)) for i, m in self.db.execute(
            "SELECT id,message FROM events WHERE id>? AND (committed=1 OR turn=?) ORDER BY id", (after, turn)
        )]

    def offload(self, text: str) -> str:
        with self.db:
            row = self.db.execute("INSERT INTO outputs(content) VALUES(?)", (text,))
        return self.ref("output", row.lastrowid)

    def ref(self, kind: str, number: int) -> str:
        return f"{self.path.stem}/{kind}:{number}"

    def source(self, ref: str) -> tuple[str, str] | None:
        namespace, _, local = ref.partition("/")
        if namespace != self.path.stem:
            return None
        kind, _, number = local.partition(":")
        if kind not in {"event", "output"} or not number.isdecimal():
            return None
        if kind == "output":
            row = self.db.execute("SELECT 'tool',content FROM outputs WHERE id=?", (int(number),)).fetchone()
        else:
            row = self.db.execute("SELECT role,content FROM events WHERE id=?", (int(number),)).fetchone()
        return tuple(row) if row else None

    def memories(self) -> list[dict]:
        return [dict(zip(("key", "text", "source"), row)) for row in self.db.execute("SELECT * FROM memory ORDER BY key")]

    def tool(self, args: dict) -> dict:
        if (not isinstance(args, dict) or args.get("operation") not in {"read", "search", "remember", "forget"}
            or any(not isinstance(v, str) for k, v in args.items() if k not in {"offset", "limit"})
            or any(type(args[k]) is not int or args[k] < 0 for k in ("offset", "limit") if k in args)):
            return {"error": "Use read/search/remember/forget, string fields, and nonnegative integer offsets/limits."}
        operation = args["operation"]
        if operation == "read":
            source = self.source(args.get("ref", ""))
            if not source:
                return {"error": "Unknown ref. Use search to find an event/output ref in this session."}
            offset, limit = max(0, args.get("offset", 0)), min(4000, max(1, args.get("limit", 2000)))
            text = source[1][offset:offset + limit]
            result = {"ref": args["ref"], "role": source[0], "text": text,
                      "next_offset": offset + len(text) if offset + len(text) < len(source[1]) else None,
                      "total_chars": len(source[1])}
            while len(json.dumps(result, ensure_ascii=False).encode()) > self.read_budget and len(result["text"]) > 1:
                result["text"] = result["text"][:len(result["text"]) // 2]
                result["next_offset"] = offset + len(result["text"])
            return result
        if operation == "search":
            words = args.get("query", "").strip().split()
            if not words or len(args.get("query", "")) > 256:
                return {"error": "Use a nonempty literal query of at most 256 characters."}
            matches = " AND ".join("instr(lower(content),lower(?))>0" for _ in words)
            rows = self.db.execute(
                "SELECT ref,role,content FROM (SELECT id,'event:'||id AS ref,role,content FROM events WHERE role IN ('user','tool') AND " + matches
                + " UNION ALL SELECT id,'output:'||id,'tool',content FROM outputs WHERE " + matches
                + ") ORDER BY id DESC LIMIT 7 OFFSET ?", (*words, *words, max(0, args.get("offset", 0)))
            ).fetchall()
            hits = []
            for ref, role, text in rows:
                start = max(0, text.lower().find(words[0].lower()) - 60)
                hit = {"ref": f"{self.path.stem}/{ref}", "role": role, "excerpt": text[start:start + 160]}
                if len(hits) == 6 or (hits and len(json.dumps(hits + [hit], ensure_ascii=False).encode()) > self.read_budget - 100):
                    break
                hits.append(hit)
            return {"hits": hits, "next_offset": args.get("offset", 0) + len(hits) if len(rows) > len(hits) else None}
        key = args.get("key", "").strip()
        if not key or len(key) > 64:
            return {"error": "Use a stable key of 1–64 characters; reuse it when correcting a note."}
        if operation == "forget":
            with self.db:
                self.db.execute("DELETE FROM memory WHERE key=?", (key,))
            return {"forgotten": key}
        text, ref = args.get("text", "").strip(), args.get("source", "")
        source = self.source(ref)
        if not text or len(text) > 1000 or not source or source[0] not in {"user", "tool"}:
            return {"error": "remember needs text (1–1000 chars) and an existing user/tool source ref; search/read first."}
        memory = [m for m in self.memories() if m["key"] != key] + [{"key": key, "text": text, "source": ref}]
        if len(memory) > 32 or estimate_tokens(json.dumps(memory, ensure_ascii=False)) > self.memory_budget:
            return {"error": "Pinned notes exceed their budget. Shorten/update/forget notes; raw evidence stays searchable."}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO memory VALUES(?,?,?)", (key, text, ref))
        return {"remembered": key, "text": text, "source": ref, "source_role": source[0]}


def admit(rows: list[tuple[int, dict]], *, budget: int = 6000, keep: int = 4, namespace: str = "") -> list[dict]:
    """Shape tool results, then mask only identical older observations; call/result pairs stay intact."""
    seen, result = set(), []
    context_calls = {c["id"] for _, m in rows for c in m.get("tool_calls", []) if c["function"]["name"] == "context"}
    recent = {i for i, m in rows if m["role"] == "tool"}
    recent = set(sorted(recent)[-keep:])
    for ref, message in reversed(rows):
        message = dict(message)
        if message["role"] == "tool":
            if ref in recent and message["tool_call_id"] in context_calls:
                result.append(message)
                continue
            text = message.get("content") or ""
            key = sha256(text.encode()).hexdigest()
            duplicate = key in seen and ref not in recent
            seen.add(key)
            message["content"] = json.dumps({"ref": f"{namespace}/event:{ref}", "duplicate": duplicate,
                "preview": "相同結果已在後續觀察保留。" if duplicate else preview(text, budget if ref in recent else min(600, budget))}, ensure_ascii=False)
        result.append(message)
    return list(reversed(result))
