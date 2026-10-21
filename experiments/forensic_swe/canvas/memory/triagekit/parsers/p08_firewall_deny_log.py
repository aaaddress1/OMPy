"""Firewall deny log parser.

Evidence: evidence/p08.log
Line format: [timestamp] host=... user=... event=...
Timestamp: US 12-hour, e.g. 03/14/2026 08:15:02 AM

Sample evidence:
    [03/12/2026 11:01:13 PM] host=LON-FS02 user=bob event=logoff
    [03/12/2026 11:32:05 PM] host=LON-FS02 user=alice event=file_write
    [03/13/2026 12:00:41 AM] host=BER-SQL01 user=carol event=logoff
    [03/13/2026 12:47:19 AM] host=NYC-WS01 user=carol event=logoff
    [03/13/2026 01:22:50 AM] host=BER-SQL01 user=carol event=logoff
    [03/13/2026 01:59:06 AM] host=BER-SQL01 user=admin event=process_start
"""

import re

from triagekit.legacy import emit

PATTERN = re.compile(r"\[(?P<logged_at>[^\]]+)\] host=(?P<device>\S+) user=(?P<principal>\S+) event=(?P<op>\S+)$")


def parse(lines):
    for line in lines:
        m = PATTERN.match(line)
        if m:
            emit(m["logged_at"], m["device"], m["principal"], m["op"])

# --- enrichment helpers (unchanged by the timeline v2 migration) ---------------------------

IOC_PATTERNS = {
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "sha256": re.compile(r"\b[a-f0-9]{64}\b"),
    "md5": re.compile(r"\b[a-f0-9]{32}\b"),
    "domain": re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|tw|cn|ru)\b", re.I),
    "unc_path": re.compile(r"\\\\[\w.-]+\\[\w$.-]+"),
    "url": re.compile(r"https?://[^\s\"']+"),
}

HOST_ROLES = {
    "NYC-WS01": "workstation",
    "NYC-DC01": "domain controller",
    "LON-FS02": "file server",
    "SFO-WS07": "workstation",
    "BER-SQL01": "database server",
    "TPE-WS03": "workstation",
    "TPE-DC02": "domain controller",
}

ACTION_SEVERITY = {
    "logon": 2,
    "logoff": 1,
    "file_read": 2,
    "file_write": 3,
    "process_start": 3,
    "logon_failed": 4,
}

# Vendor fields that may follow the core columns in extended exports; not part of the timeline.
EXTRA_FIELDS = {
    "logon_type": "interactive / network / service",
    "rule": "matched policy rule",
    "job_id": "vendor job identifier",
    "src_ip": "client address, IPv4",
    "vendor_id": "USB VID:PID",
    "query": "DNS question name",
    "session_id": "opaque session identifier, hex",
    "status": "vendor status string",
    "bytes_in": "bytes received by client",
}


def extract_iocs(text):
    """Return every indicator found in a raw evidence line, grouped by type."""
    found = {}
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
    return weight >= 3


def split_extended(line, separator=" "):
    """Split an extended export line into core text and key=value vendor fields."""
    core, extras = [], {}
    for token in line.split(separator):
        key, sep, value = token.partition("=")
        if sep and key in EXTRA_FIELDS:
            extras[key] = value
        else:
            core.append(token)
    return separator.join(core), extras


def triage_summary(lines):
    """Count interesting lines and collect indicators for the analyst notes."""
    interesting, indicators = 0, {}
    for line in lines:
        match = PATTERN.match(line)
        if not match:
            continue
        values = list(match.groupdict().values())
        if is_interesting(values[1] if len(values) > 1 else "", values[-1]):
            interesting += 1
        for kind, items in extract_iocs(line).items():
            indicators.setdefault(kind, set()).update(items)
    return {"interesting": interesting, "indicators": {k: sorted(v) for k, v in indicators.items()}}
