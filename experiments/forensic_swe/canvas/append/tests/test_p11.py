from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p11_kerberos_ticket_log import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p11.log").read_text(encoding="utf-8").splitlines()


def test_p11():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p11" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[0]
    assert (probe.host, probe.account, probe.action) == ('NYC-DC01', 'carol', 'file_write')
    assert probe.ts_utc == datetime(2026, 3, 15, 20, 32, 11, tzinfo=timezone.utc)
