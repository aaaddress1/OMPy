from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p05_o365_unified_audit_log import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p05.log").read_text(encoding="utf-8").splitlines()


def test_p05():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p05" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[1]
    assert (probe.host, probe.account, probe.action) == ('BER-SQL01', 'svc_backup', 'logon'), "Account 'svc_bkp' is the renamed 'svc_backup'; normalize it to 'svc_backup' in every parser."
    assert probe.ts_utc == datetime(2026, 3, 13, 13, 25, 0, tzinfo=timezone.utc)
