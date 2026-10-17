"""Read-only operational signals. Never return capture text or provider bodies."""

from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx

from wortling.config import Settings
from wortling.db import Database
from wortling.llm.client import LLMClient


def operational_status(settings: Settings, *, probe_provider=False, transport=None) -> dict:
    timestamp = datetime.now(UTC)
    issues = []
    metrics = {}

    def age(value):
        return (timestamp - datetime.fromisoformat(value)).total_seconds()

    try:
        metrics["worker_heartbeat_age_seconds"] = round(
            timestamp.timestamp() - (settings.data_dir / "worker-heartbeat").stat().st_mtime
        )
        if metrics["worker_heartbeat_age_seconds"] > 900:
            issues.append("worker_heartbeat_stale")
    except FileNotFoundError:
        issues.append("worker_heartbeat_missing")
    try:
        with Database(settings.data_dir).connect() as conn:
            jobs = conn.execute(
                "SELECT j.status,j.next_attempt_at,j.lease_until,c.user_id "
                "FROM jobs j JOIN captures c ON c.id=j.capture_id"
            ).fetchall()
            metrics["jobs"] = dict(Counter(row["status"] for row in jobs))
            metrics["last_llm_success_at"] = conn.execute(
                "SELECT max(created_at) FROM llm_calls WHERE outcome='ok'"
            ).fetchone()[0]
            metrics["llm_outcomes_last_hour"] = dict(
                conn.execute(
                    "SELECT outcome,count(*) FROM llm_calls WHERE created_at>=? GROUP BY outcome",
                    ((timestamp - timedelta(hours=1)).isoformat(),),
                ).fetchall()
            )
            overdue = [
                row
                for row in jobs
                if row["status"] == "pending" and age(row["next_attempt_at"]) > 900
            ]
            metrics["overdue_pending_jobs"] = len(overdue)
            if overdue:
                issues.append("pending_jobs_overdue")
            if any(row["status"] == "failed" for row in jobs):
                issues.append("failed_jobs")
            if any(
                row["status"] == "running"
                and (not row["lease_until"] or age(row["lease_until"]) > 0)
                for row in jobs
            ):
                issues.append("running_job_lease_expired")
            metrics["unacknowledged_alerts"] = dict(
                conn.execute(
                    "SELECT kind,count(*) FROM alerts WHERE acknowledged_at IS NULL GROUP BY kind"
                ).fetchall()
            )
            if metrics["unacknowledged_alerts"]:
                issues.append("application_alerts_unacknowledged")
            backup = conn.execute(
                "SELECT created_at FROM backup_runs WHERE status='complete' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            metrics["backup_age_seconds"] = round(age(backup[0])) if backup else None
            if not backup or age(backup[0]) > 30 * 3600:
                issues.append("backup_missing_or_stale")
            states = {row["user_id"]: row for row in conn.execute("SELECT * FROM scheduler_state")}
            overdue_syncs = 0
            for user in settings.users:
                collection = settings.data_dir / "collections" / user.id / "collection.anki2"
                if collection.exists():
                    state = states.get(user.id)
                    if not state or age(state["next_sync_at"]) > 1800:
                        overdue_syncs += 1
            metrics["overdue_syncs"] = overdue_syncs
            if overdue_syncs:
                issues.append("scheduled_sync_overdue")
    except Exception:
        # Exception text can include data. The host monitor still sees a failed probe.
        issues.append("database_probe_failed")
    if probe_provider:
        try:
            token = LLMClient(settings, Database(settings.data_dir)).configured_token()
            with httpx.Client(transport=transport, timeout=20) as client:
                response = client.get(
                    settings.llm.base_url.rstrip("/") + "/models",
                    headers={"Authorization": "Bearer " + token},
                )
            metrics["provider_http_status"] = response.status_code
            if not response.is_success:
                issues.append("provider_auth_or_availability_failed")
            else:
                available = {model["id"] for model in response.json()["data"]}
                if set(settings.llm.models.values()) - available:
                    issues.append("configured_models_unavailable")
        except Exception:
            issues.append("provider_probe_failed")
    return {
        "status": "degraded" if issues else "ok",
        "checked_at": timestamp.isoformat(),
        "issues": issues,
        "metrics": metrics,
    }
