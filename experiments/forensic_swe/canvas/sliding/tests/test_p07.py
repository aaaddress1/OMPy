from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p07_sql_server_audit import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p07.log").read_text(encoding="utf-8").splitlines()


def test_p07():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p07" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[0]
    assert (probe.host, probe.account, probe.action) == ('NYC-WS01', 'alice', 'logon_failed'), 'Numeric action codes must be normalized in every parser: 4624=logon, 4625=logon_failed, 4634=logoff.'
    assert probe.ts_utc == datetime(2026, 3, 19, 10, 2, 39, tzinfo=timezone.utc)
