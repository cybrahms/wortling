"""Recording and clearing application alerts.

An alert is a failure recorded in the database, and a record with no way out is
how one rejected request paged the operator every five minutes for two days: a
`sync_failed` alert stayed open after the sync it complained about had started
working again, and the operational check counts every open alert as a standing
issue.

Two rules live here so that every writer of `alerts` agrees on them.

A success acknowledges the alerts its own kind left behind. That is what
`capture_failed` has always done when its capture finally processes; a
successful sync and a completed write session do it through `acknowledge`.

A kind with no recovery to wait for expires. A transient failure that was never
cleared stops counting as an issue after its horizon, so a fixed problem leaves
the operational check quiet even when nobody remembered to acknowledge it. The
row stays as history: what stops is the standing issue, not the record.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from wortling.db import now

# Kinds that describe one failed attempt at something that is retried on a
# schedule, and the horizon after which an uncleared one stops counting. These
# are deliberately longer than a retry interval and shorter than a working day:
# six hours is four scheduled retries of a sync that runs every fifteen minutes
# while failing, and half a night's sleep for whoever reads the report.
TRANSIENT = {
    "sync_failed": timedelta(hours=6),
    "write_failed": timedelta(hours=6),
}


def acknowledge(conn: sqlite3.Connection, user_id: str, kind: str, *, at: str | None = None) -> int:
    """Clear every open alert of one kind for one learner, on recovery.

    Returns how many were cleared, which is what a test can assert and what a
    caller logging the recovery would want.
    """
    cursor = conn.execute(
        "UPDATE alerts SET acknowledged_at=? "
        "WHERE user_id=? AND kind=? AND acknowledged_at IS NULL",
        (at or now(), user_id, kind),
    )
    return cursor.rowcount


def expired(kind: str, created_at: str, at: datetime) -> bool:
    """Whether an open alert of this kind has passed its horizon.

    An unknown kind never expires: a kind nobody taught this module a horizon
    for is one whose recovery has not been reasoned about, and silently
    discarding it would hide the failure instead of reporting it.
    """
    horizon = TRANSIENT.get(kind)
    if horizon is None:
        return False
    try:
        recorded = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return False
    return at - recorded >= horizon


def standing(conn: sqlite3.Connection, *, at: datetime | None = None) -> dict[str, int]:
    """Open alerts that still count as an issue, by kind.

    Expired transient alerts are left out. A caller that wants the raw open
    count, such as the learner-facing report, queries the table itself: the
    record is history, and this function answers only "is this a standing
    issue".
    """
    moment = at or datetime.now(UTC)
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT kind,created_at FROM alerts WHERE acknowledged_at IS NULL"):
        if expired(row["kind"], row["created_at"], moment):
            continue
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return counts
