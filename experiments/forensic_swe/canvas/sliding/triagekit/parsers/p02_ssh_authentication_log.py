"""SSH authentication log parser.

Evidence: evidence/p02.log
Line format: host,action,account,timestamp (CSV, note the column order)
Timestamp: US 12-hour, e.g. 03/14/2026 08:15:02 AM

Sample evidence:
    BER-SQL01,process_start,svc_web,03/15/2026 06:47:18 PM
    NYC-WS01,logoff,svc_web,03/15/2026 07:27:37 PM
    SFO-WS07,logoff,svc_web,03/15/2026 08:15:51 PM
    LON-FS02,file_write,alice,03/15/2026 08:40:35 PM
    NYC-WS01,process_start,bob,03/15/2026 09:27:32 PM
    LON-FS02,logon,svc_web,03/15/2026 10:09:25 PM
"""

import re

from triagekit.legacy import emit

PATTERN = re.compile(r"(?P<computer>[^,]+),(?P<event>[^,]+),(?P<who>[^,]+),(?P<ts>.+)$")


def parse(lines):
    for line in lines:
        m = PATTERN.match(line)
        if m:
            emit(m["ts"], m["computer"], m["who"], m["event"])

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
    "hash": "SHA-256 of the image",
    "session_id": "opaque session identifier, hex",
    "query": "DNS question name",
    "bytes_out": "bytes sent by client",
    "logon_type": "interactive / network / service",
    "vendor_id": "USB VID:PID",
    "job_id": "vendor job identifier",
    "parent": "image path of the parent process",
    "process": "image path of the acting process",
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
