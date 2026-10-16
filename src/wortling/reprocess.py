import re

from wortling.db import Database, now


def reprocess(
    db: Database,
    user_id: str,
    since: str = "v1",
    *,
    capture_id: str | None = None,
    explicit_lookup: bool = False,
) -> int:
    if explicit_lookup and not capture_id:
        raise ValueError("Explicit lookup requires one --capture-id")
    match = re.fullmatch(r"(?:(normalize|extract|judge|generate)\.)?(v[0-9]+)", since)
    if not match:
        raise ValueError("--since must be a version such as v1 or extract.v1")
    stage, version = match.groups()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if capture_id:
            rows = conn.execute(
                "SELECT c.id FROM captures c JOIN jobs j ON j.capture_id=c.id "
                "WHERE c.user_id=? AND c.id=? AND j.status!='running'",
                (user_id, capture_id),
            ).fetchall()
            if not rows:
                raise ValueError("Capture not found for learner, or still processing")
        else:
            rows = conn.execute(
                "SELECT DISTINCT c.id FROM captures c JOIN processing_runs r ON r.capture_id=c.id "
                "JOIN jobs j ON j.capture_id=c.id WHERE c.user_id=? AND j.status!='running' AND ("
                "EXISTS (SELECT 1 FROM decisions d WHERE d.run_id=r.id AND d.prompt_version=? "
                "AND (? IS NULL OR d.stage=?)) OR EXISTS "
                "(SELECT 1 FROM llm_calls l WHERE l.run_id=r.id "
                "AND l.prompt_version=? AND (? IS NULL OR l.stage=?)))",
                (user_id, version, stage, stage, version, stage, stage),
            ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE cards SET status='dropped',updated_at=? "
                "WHERE user_id=? AND anki_note_id IS NULL "
                "AND status IN ('pending','blocked') AND id IN "
                "(SELECT card_id FROM encounters WHERE capture_id=?)",
                (now(), user_id, row[0]),
            )
            conn.execute(
                "UPDATE jobs SET status='pending',attempts=0,reprocess=1,"
                "next_attempt_at=?,error=NULL,explicit_lookup=MAX(explicit_lookup,?) "
                "WHERE capture_id=?",
                (now(), int(explicit_lookup), row[0]),
            )
            conn.execute(
                "UPDATE captures SET status='reprocess_pending',error=NULL WHERE id=?", (row[0],)
            )
    return len(rows)
