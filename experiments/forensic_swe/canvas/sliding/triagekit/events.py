"""Timeline v2 event schema."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Event:
    ts_utc: datetime
    host: str
    account: str
    action: str
    source: str
