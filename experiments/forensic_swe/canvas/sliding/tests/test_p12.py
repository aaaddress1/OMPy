from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p12_smb_share_access import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p12.log").read_text(encoding="utf-8").splitlines()


def test_p12():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p12" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[0]
    assert (probe.host, probe.account, probe.action) == ('NYC-WS01', 'admin', 'logoff')
    assert probe.ts_utc == datetime(2026, 3, 16, 0, 34, 1, tzinfo=timezone.utc)
