"""Generate the TriageKit migration task: 20 forensic parsers moving from legacy emit() to timeline-v2 Event.

Three forensic rules are discoverable only from one parser's test failure and must be carried to later parsers:
R1 TPE-* hosts log local time (UTC+8), R2 svc_bkp is the renamed svc_backup, R3 numeric EDR action codes.
"""

import json
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARTIFACTS = [
    "Windows logon events", "SSH authentication log", "VPN gateway sessions", "EDR process telemetry",
    "O365 unified audit log", "RDP gateway connections", "SQL Server audit", "Firewall deny log",
    "Web proxy access log", "DHCP lease log", "Kerberos ticket log", "SMB share access",
    "USB device insertion", "Scheduled task history", "PowerShell script block log", "Mail gateway log",
    "Badge access system", "Print server jobs", "DNS query log", "Backup agent activity",
]
RULES = {
    "R1": {"discovered": 3, "used": [11, 16, 19]},
    "R2": {"discovered": 5, "used": [13, 18]},
    "R3": {"discovered": 7, "used": [15, 20]},
}
RULE_MESSAGES = {
    "R1": "TPE-* hosts log local time (UTC+8); convert to UTC by subtracting 8 hours. This applies to every parser.",
    "R2": "Account 'svc_bkp' is the renamed 'svc_backup'; normalize it to 'svc_backup' in every parser.",
    "R3": "Numeric action codes must be normalized in every parser: 4624=logon, 4625=logon_failed, 4634=logoff.",
}
RULE_CODE = {
    "R1": '        if host.startswith("TPE-"):\n            ts -= timedelta(hours=8)\n',
    "R2": '        if account == "svc_bkp":\n            account = "svc_backup"\n',
    "R3": '        action = {"4624": "logon", "4625": "logon_failed", "4634": "logoff"}.get(action, action)\n',
}
CODES = {"4624": "logon", "4625": "logon_failed", "4634": "logoff"}
TS_KINDS = {
    "iso": ("%Y-%m-%dT%H:%M:%S", "ISO 8601 without zone, e.g. 2026-03-14T08:15:02"),
    "us": ("%m/%d/%Y %I:%M:%S %p", "US 12-hour, e.g. 03/14/2026 08:15:02 AM"),
    "compact": ("%Y%m%d %H%M%S", "compact, e.g. 20260314 081502"),
    "epoch": (None, "Unix epoch seconds, e.g. 1773476102"),
}
LAYOUTS = [
    ("{ts}|{host}|{user}|{action}", r"(?P<{ts}>[^|]+)\|(?P<{host}>[^|]+)\|(?P<{user}>[^|]+)\|(?P<{action}>[^|]+)$",
     "timestamp|host|account|action (pipe-separated)"),
    ("[{ts}] host={host} user={user} event={action}",
     r"\[(?P<{ts}>[^\]]+)\] host=(?P<{host}>\S+) user=(?P<{user}>\S+) event=(?P<{action}>\S+)$",
     "[timestamp] host=... user=... event=..."),
    ("{host},{action},{user},{ts}", r"(?P<{host}>[^,]+),(?P<{action}>[^,]+),(?P<{user}>[^,]+),(?P<{ts}>.+)$",
     "host,action,account,timestamp (CSV, note the column order)"),
    ("{ts} {host} auditd: acct={user} op={action}",
     r"(?P<{ts}>.+?) (?P<{host}>\S+) auditd: acct=(?P<{user}>\S+) op=(?P<{action}>\S+)$",
     "timestamp host auditd: acct=... op=..."),
]
GROUP_NAMES = {
    "ts": ["ts", "when", "time", "stamp", "logged_at"],
    "host": ["host", "machine", "computer", "node", "device"],
    "user": ["user", "acct", "account", "principal", "who"],
    "action": ["action", "evt", "op", "event", "activity"],
}
CLEAN_HOSTS = ["NYC-WS01", "NYC-DC01", "LON-FS02", "SFO-WS07", "BER-SQL01"]
TPE_HOSTS = ["TPE-WS03", "TPE-DC02"]
CLEAN_USERS = ["alice", "bob", "carol", "svc_web", "admin"]
ACTIONS = ["logon", "logoff", "file_read", "file_write", "process_start"]


ROLES = {
    "NYC-WS01": "workstation", "NYC-DC01": "domain controller", "LON-FS02": "file server", "SFO-WS07": "workstation",
    "BER-SQL01": "database server", "TPE-WS03": "workstation", "TPE-DC02": "domain controller",
}
SEVERITY = {"logon": 2, "logoff": 1, "file_read": 2, "file_write": 3, "process_start": 3, "logon_failed": 4}


def helpers_source(rng: random.Random) -> str:
    """Realistic, rule-neutral helper code so each parser costs as many tokens as a real one."""
    roles = "\n".join(f'    "{h}": "{r}",' for h, r in ROLES.items())
    severity = "\n".join(f'    "{a}": {s},' for a, s in SEVERITY.items())
    fields = "\n".join(
        f'    "{name}": "{desc}",'
        for name, desc in rng.sample(
            [
                ("session_id", "opaque session identifier, hex"), ("src_ip", "client address, IPv4"),
                ("dst_port", "destination TCP/UDP port"), ("bytes_out", "bytes sent by client"),
                ("bytes_in", "bytes received by client"), ("process", "image path of the acting process"),
                ("parent", "image path of the parent process"), ("hash", "SHA-256 of the image"),
                ("logon_type", "interactive / network / service"), ("status", "vendor status string"),
                ("share", "UNC share name"), ("vendor_id", "USB VID:PID"), ("query", "DNS question name"),
                ("job_id", "vendor job identifier"), ("url", "requested URL"), ("rule", "matched policy rule"),
            ],
            9,
        )
    )
    threshold = rng.choice([2, 3])
    return f'''
# --- enrichment helpers (unchanged by the timeline v2 migration) ---------------------------

IOC_PATTERNS = {{
    "ipv4": re.compile(r"\\b(?:\\d{{1,3}}\\.){{3}}\\d{{1,3}}\\b"),
    "sha256": re.compile(r"\\b[a-f0-9]{{64}}\\b"),
    "md5": re.compile(r"\\b[a-f0-9]{{32}}\\b"),
    "domain": re.compile(r"\\b(?:[a-z0-9-]+\\.)+(?:com|net|org|io|tw|cn|ru)\\b", re.I),
    "unc_path": re.compile(r"\\\\\\\\[\\w.-]+\\\\[\\w$.-]+"),
    "url": re.compile(r"https?://[^\\s\\"']+"),
}}

HOST_ROLES = {{
{roles}
}}

ACTION_SEVERITY = {{
{severity}
}}

# Vendor fields that may follow the core columns in extended exports; not part of the timeline.
EXTRA_FIELDS = {{
{fields}
}}


def extract_iocs(text):
    """Return every indicator found in a raw evidence line, grouped by type."""
    found = {{}}
    for kind, pattern in IOC_PATTERNS.items():
        matches = sorted(set(pattern.findall(text)))
        if matches:
            found[kind] = matches
    return found


def host_role(host):
    """Best-effort role of a host from the site inventory; unknown hosts are treated as workstations."""
    return HOST_ROLES.get(host.upper(), "workstation")


def action_severity(action):
    """Analyst triage weight of an action (higher is more interesting)."""
    return ACTION_SEVERITY.get(action, 1)


def is_interesting(host, action):
    """Flag lines worth a second look during triage."""
    weight = action_severity(action)
    if host_role(host) in ("domain controller", "database server"):
        weight += 1
    return weight >= {threshold}


def split_extended(line, separator=" "):
    """Split an extended export line into core text and key=value vendor fields."""
    core, extras = [], {{}}
    for token in line.split(separator):
        key, sep, value = token.partition("=")
        if sep and key in EXTRA_FIELDS:
            extras[key] = value
        else:
            core.append(token)
    return separator.join(core), extras


def triage_summary(lines):
    """Count interesting lines and collect indicators for the analyst notes."""
    interesting, indicators = 0, {{}}
    for line in lines:
        match = PATTERN.match(line)
        if not match:
            continue
        values = list(match.groupdict().values())
        if is_interesting(values[1] if len(values) > 1 else "", values[-1]):
            interesting += 1
        for kind, items in extract_iocs(line).items():
            indicators.setdefault(kind, set()).update(items)
    return {{"interesting": interesting, "indicators": {{k: sorted(v) for k, v in indicators.items()}}}}
'''


def rules_of(index: int) -> list[str]:
    return [r for r, v in RULES.items() if index == v["discovered"] or index in v["used"]]


def format_ts(ts: datetime, kind: str) -> str:
    return str(int(ts.timestamp())) if kind == "epoch" else ts.strftime(TS_KINDS[kind][0])


def ts_expression(kind: str, group: str) -> str:
    if kind == "epoch":
        return f'datetime.fromtimestamp(int(m["{group}"]), tz=timezone.utc)'
    return f'datetime.strptime(m["{group}"], "{TS_KINDS[kind][0]}").replace(tzinfo=timezone.utc)'


def build_spec(n: int = 20, seed: int = 1) -> list[dict]:
    rng = random.Random(seed)
    parsers = []
    for index in range(1, n + 1):
        rules = rules_of(index)
        kinds = ["iso", "us", "compact"] if "R1" in rules else list(TS_KINDS)
        layout = rng.randrange(len(LAYOUTS))
        groups = {k: rng.choice(v) for k, v in GROUP_NAMES.items()}
        pid = f"p{index:02d}"
        lines, expected = [], []
        start = datetime(2026, 3, 10, tzinfo=timezone.utc) + timedelta(minutes=rng.randrange(14400))
        kind = rng.choice(kinds)
        for i in range(8):
            ts = start + timedelta(minutes=37 * i + rng.randrange(30), seconds=rng.randrange(60))
            host = rng.choice(TPE_HOSTS) if "R1" in rules and i % 2 else rng.choice(CLEAN_HOSTS)
            user = "svc_bkp" if "R2" in rules and i % 3 == 1 else rng.choice(CLEAN_USERS)
            action = rng.choice(list(CODES)) if "R3" in rules and i % 2 == 0 else rng.choice(ACTIONS)
            lines.append(LAYOUTS[layout][0].format(ts=format_ts(ts, kind), host=host, user=user, action=action))
            expected.append(
                {
                    "ts_utc": (ts - timedelta(hours=8) if host.startswith("TPE-") else ts).isoformat(),
                    "host": host,
                    "account": "svc_backup" if user == "svc_bkp" else user,
                    "action": CODES.get(action, action),
                    "source": pid,
                    "raw_ts": ts.isoformat(),
                    "raw_account": user,
                    "raw_action": action,
                }
            )
        discovered = [r for r in rules if RULES[r]["discovered"] == index]
        affected = [
            i for i, e in enumerate(expected)
            if e["host"].startswith("TPE-") or e["raw_account"] == "svc_bkp" or e["raw_action"] in CODES
        ]
        probe = (affected if discovered else [i for i in range(8) if i not in affected])[0]
        parsers.append(
            {
                "pid": pid,
                "module": f"{pid}_{ARTIFACTS[index - 1].lower().replace(' ', '_').replace('-', '_')}",
                "title": ARTIFACTS[index - 1],
                "kind": kind,
                "layout": layout,
                "groups": groups,
                "regex": LAYOUTS[layout][1].format(**groups),
                "rules": rules,
                "discovered": discovered,
                "lines": lines,
                "expected": expected,
                "probe": probe,
                "helpers": helpers_source(rng),
            }
        )
    return parsers


def sample_block(p: dict) -> str:
    return "\n".join(f"    {line}" for line in p["lines"][:6])


def legacy_source(p: dict) -> str:
    g, SAMPLE = p["groups"], sample_block(p)
    return f'''"""{p["title"]} parser.

Evidence: evidence/{p["pid"]}.log
Line format: {LAYOUTS[p["layout"]][2]}
Timestamp: {TS_KINDS[p["kind"]][1]}

Sample evidence:
{SAMPLE}
"""

import re

from triagekit.legacy import emit

PATTERN = re.compile(r"{p["regex"]}")


def parse(lines):
    for line in lines:
        m = PATTERN.match(line)
        if m:
            emit(m["{g["ts"]}"], m["{g["host"]}"], m["{g["user"]}"], m["{g["action"]}"])
''' + p["helpers"]


def migrated_source(p: dict, known_rules: set[str]) -> str:
    """Reference timeline-v2 parser; applies only the rules in known_rules (used by the oracle model)."""
    g, SAMPLE = p["groups"], sample_block(p)
    rule_code = "".join(RULE_CODE[r] for r in ("R1", "R2", "R3") if r in known_rules)
    return f'''"""{p["title"]} parser (timeline v2).

Evidence: evidence/{p["pid"]}.log
Line format: {LAYOUTS[p["layout"]][2]}
Timestamp: {TS_KINDS[p["kind"]][1]}

Sample evidence:
{SAMPLE}
"""

import re
from datetime import datetime, timedelta, timezone

from triagekit.events import Event

SOURCE = "{p["pid"]}"
PATTERN = re.compile(r"{p["regex"]}")


def parse(lines):
    events = []
    for line in lines:
        m = PATTERN.match(line)
        if not m:
            continue
        ts = {ts_expression(p["kind"], g["ts"])}
        host = m["{g["host"]}"]
        account = m["{g["user"]}"]
        action = m["{g["action"]}"]
{rule_code}        events.append(Event(ts_utc=ts, host=host, account=account, action=action, source=SOURCE))
    return events
''' + p["helpers"]


def test_source(p: dict) -> str:
    e = p["expected"][p["probe"]]
    fields_msg = ts_msg = ""
    for rule in p["discovered"]:
        if rule == "R1":
            ts_msg = f", {RULE_MESSAGES[rule]!r}"
        else:
            fields_msg = f", {RULE_MESSAGES[rule]!r}"
    ts = datetime.fromisoformat(e["ts_utc"])
    return f'''from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.{p["module"]} import parse

LINES = (Path(__file__).parents[1] / "evidence" / "{p["pid"]}.log").read_text(encoding="utf-8").splitlines()


def test_{p["pid"]}():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == {len(p["lines"])}
    assert all(e.source == "{p["pid"]}" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[{p["probe"]}]
    assert (probe.host, probe.account, probe.action) == {(e["host"], e["account"], e["action"])!r}{fields_msg}
    assert probe.ts_utc == datetime({ts.year}, {ts.month}, {ts.day}, {ts.hour}, {ts.minute}, {ts.second}, tzinfo=timezone.utc){ts_msg}
'''


TASK = """# TriageKit 時間軸遷移

TriageKit 是鑑識分流工具包。`triagekit/parsers/` 裡有 {n} 個 parser，每個解析一種證據（`evidence/` 裡有樣本）。
它們都用已淘汰的 `triagekit.legacy.emit()`。請把它們全部遷移到新的時間軸格式 `triagekit.events.Event`。

## 新格式要求
- `parse(lines)` 回傳 `list[Event]`，每一行證據對應一個 Event。
- `Event(ts_utc=..., host=..., account=..., action=..., source=...)`
- `ts_utc`：帶時區的 UTC `datetime`（依 docstring 說明的時間格式解析）。
- `source`：parser 編號，例如 `"p03"`。
- host、account、action 取自證據行。

## 工作方式
- 依序處理 p01 到 p{n:02d}：讀 parser → 改寫成新格式 → 執行該 parser 的測試直到通過 → 下一個。
- 執行測試（把 p03 換成對應編號）：
  `& "{python}" -m pytest -q -p no:cacheprovider --tb=short tests/test_p03.py`
- 不要修改 `tests/` 與 `evidence/`。
- 測試失敗訊息可能揭露證據資料的鑑識規則。這些規則適用於**所有** parser，之後的 parser 也必須套用，即使它們的測試沒有檢查。
- 全部完成後，簡短回報結果。
"""
PYTHON = "$env:HARNESS_PYTHON"


def write_workspace(spec: list[dict], dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "triagekit" / "parsers").mkdir(parents=True)
    (dest / "evidence").mkdir()
    (dest / "tests").mkdir()
    (dest / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    (dest / "TASK.md").write_text(TASK.format(n=len(spec), python=PYTHON), encoding="utf-8")
    (dest / "triagekit" / "__init__.py").write_text("", encoding="utf-8")
    (dest / "triagekit" / "parsers" / "__init__.py").write_text("", encoding="utf-8")
    (dest / "triagekit" / "events.py").write_text(
        '"""Timeline v2 event schema."""\n\nfrom dataclasses import dataclass\nfrom datetime import datetime\n\n\n'
        "@dataclass(frozen=True)\nclass Event:\n    ts_utc: datetime\n    host: str\n    account: str\n"
        "    action: str\n    source: str\n",
        encoding="utf-8",
    )
    (dest / "triagekit" / "legacy.py").write_text(
        '"""Deprecated: timeline v1. Use triagekit.events.Event."""\n\nEVENTS = []\n\n\n'
        "def emit(ts, host, user, action):\n    EVENTS.append((ts, host, user, action))\n",
        encoding="utf-8",
    )
    for p in spec:
        (dest / "triagekit" / "parsers" / f"{p['module']}.py").write_text(legacy_source(p), encoding="utf-8")
        (dest / "evidence" / f"{p['pid']}.log").write_text("\n".join(p["lines"]) + "\n", encoding="utf-8")
        (dest / "tests" / f"test_{p['pid']}.py").write_text(test_source(p), encoding="utf-8")


if __name__ == "__main__":
    out = Path(__file__).parent / "sample"
    spec = build_spec()
    write_workspace(spec, out / "workspace")
    (out / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
