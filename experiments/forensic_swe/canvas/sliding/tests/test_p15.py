from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p15_powershell_script_block_log import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p15.log").read_text(encoding="utf-8").splitlines()


def test_p15():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p15" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[1]
    assert (probe.host, probe.account, probe.action) == ('BER-SQL01', 'carol', 'logoff')
    assert probe.ts_utc == datetime(2026, 3, 14, 2, 12, 16, tzinfo=timezone.utc)
