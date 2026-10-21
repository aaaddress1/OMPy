from datetime import datetime, timezone
from pathlib import Path

from triagekit.events import Event
from triagekit.parsers.p04_edr_process_telemetry import parse

LINES = (Path(__file__).parents[1] / "evidence" / "p04.log").read_text(encoding="utf-8").splitlines()


def test_p04():
    events = parse(LINES)
    assert isinstance(events, list) and all(isinstance(e, Event) for e in events), "parse() must return list[Event]"
    assert len(events) == 8
    assert all(e.source == "p04" for e in events), "Event.source must be the parser id"
    assert all(e.ts_utc.utcoffset() is not None and e.ts_utc.utcoffset().total_seconds() == 0 for e in events), (
        "ts_utc must be timezone-aware UTC"
    )
    probe = events[0]
    assert (probe.host, probe.account, probe.action) == ('LON-FS02', 'svc_web', 'file_read')
    assert probe.ts_utc == datetime(2026, 3, 12, 22, 2, 17, tzinfo=timezone.utc)
