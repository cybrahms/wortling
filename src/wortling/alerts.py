"""Recording and clearing application alerts.

An alert is a failure recorded in the database, and a record with no way out is
how one rejected request paged the operator every five minutes for two days: a
`sync_failed` alert stayed open after the sync it complained about had started
working again, and the operational check counts every open alert as a standing
issue.

Two rules live here so that every writer of `alerts` agrees on them.

A success acknowledges the alerts its own kind left behind. That is what
`capture_failed` has always done when its capture finally processes; a
successful sync, a completed write session and a finished backup do it through
`acknowledge`.

A kind with no recovery to wait for expires. A transient failure that was never
cleared stops counting as an issue after its horizon, so a fixed problem leaves
the operational check quiet even when nobody remembered to acknowledge it, and a
kind whose condition ends when its period does, like the monthly budget, stops
counting once that period is over. The row stays as history: what stops is the
standing issue, not the record.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from wortling.db import now

# The horizon after which an uncleared alert of a kind stops counting. A
# timedelta is a retry schedule's patience: deliberately longer than a retry
# interval and shorter than a working day, six hours being four scheduled
# retries of a sync that runs every fifteen minutes while failing, and half a
# night's sleep for whoever reads the report. MONTH is a condition that ends
# with its period instead of on a clock.
MONTH = "month"

HORIZONS: dict[str, timedelta | str] = {
    "sync_failed": timedelta(hours=6),
    "write_failed": timedelta(hours=6),
    "monthly_budget": MONTH,
}


def acknowledge(
    conn: sqlite3.Connection, user_id: str | None, kind: str, *, at: str | None = None
) -> int:
    """Clear every open alert of one kind for one learner, on recovery.

    `user_id` is None for a kind that belongs to nobody, a backup failing on the
    whole installation rather than on one learner. The comparison is `IS` and
    not `=` because `user_id = NULL` is never true, which would leave the global
    kinds clearing nothing at all.

    Returns how many were cleared, which is what a test can assert and what a
    caller logging the recovery would want.
    """
    cursor = conn.execute(
        "UPDATE alerts SET acknowledged_at=? "
        "WHERE user_id IS ? AND kind=? AND acknowledged_at IS NULL",
        (at or now(), user_id, kind),
    )
    return cursor.rowcount


def expired(kind: str, created_at: str, at: datetime) -> bool:
    """Whether an open alert of this kind has passed its horizon.

    An unknown kind never expires: a kind nobody taught this module a horizon
    for is one whose recovery has not been reasoned about, and silently
    discarding it would hide the failure instead of reporting it. A timestamp
    this cannot read or cannot compare is treated the same way: an alert nobody
    can date is not evidence that the condition went away.
    """
    horizon = HORIZONS.get(kind)
    if horizon is None:
        return False
    try:
        recorded = datetime.fromisoformat(created_at)
        if horizon == MONTH:
            return (at.year, at.month) > (recorded.year, recorded.month)
        return at - recorded >= horizon
    except (TypeError, ValueError):
        return False


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
