from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p03_vpn_gateway_sessions import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p03.log").read_text(encoding="utf-8").splitlines()


def test_p03():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p03" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[1]
    assert (probe.host, probe.account, probe.action) == ('TPE-WS03', 'svc_web', 'file_read')
    assert probe.ts_utc == datetime(2026, 3, 14, 1, 29, 27, tzinfo=timezone.utc), 'TPE-* hosts log local time (UTC+8); convert to UTC by subtracting 8 hours. This applies to every parser.'
