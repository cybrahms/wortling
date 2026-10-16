from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wortling.config import Learner
from wortling.db import Database, now


def learner_report(db: Database, user: Learner) -> dict:
    with db.connect() as conn:
        captures = [
            dict(row)
            for row in conn.execute(
                "SELECT c.id,c.received_at,c.text,c.url,c.image_mime,c.status,c.error,"
                "j.status AS processing_status FROM captures c "
                "LEFT JOIN jobs j ON j.capture_id=c.id "
                "WHERE c.user_id=? ORDER BY c.received_at DESC LIMIT 100",
                (user.id,),
            )
        ]
        cards = []
        for row in conn.execute(
            "SELECT * FROM cards WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (user.id,)
        ):
            card = dict(row)
            card["fields"] = json.loads(card.pop("fields_json"))
            cards.append(card)
        decisions = [
            dict(row)
            for row in conn.execute(
                "SELECT d.*,i.lemma,i.sentence FROM decisions d "
                "JOIN processing_runs r ON r.id=d.run_id JOIN captures c ON c.id=r.capture_id "
                "LEFT JOIN items i ON i.id=d.item_id WHERE c.user_id=? "
                "ORDER BY d.id DESC LIMIT 500",
                (user.id,),
            )
        ]
        alerts = [
            dict(row)
            for row in conn.execute(
                "SELECT a.*,c.text AS capture_text,c.url AS capture_url "
                "FROM alerts a LEFT JOIN captures c ON c.id=a.capture_id "
                "WHERE (a.user_id=? OR a.user_id IS NULL) "
                "AND a.acknowledged_at IS NULL ORDER BY a.id DESC",
                (user.id,),
            )
        ]
        index = dict(
            conn.execute(
                "SELECT COUNT(*) AS entries,MAX(refreshed_at) AS refreshed_at "
                "FROM known_index WHERE user_id=?",
                (user.id,),
            ).fetchone()
        )
        month = now()[:7]
        usage = dict(
            conn.execute(
                "SELECT COALESCE(SUM(tokens_in),0) AS tokens_in,"
                "COALESCE(SUM(tokens_out),0) AS tokens_out,"
                "COALESCE(SUM(cost_usd),0) AS cost_usd FROM llm_calls "
                "WHERE user_id=? AND created_at>=?",
                (user.id, month),
            ).fetchone()
        )
    return {
        "learner": user.display_name,
        "processing_enabled": user.processing_enabled,
        "writes_enabled": user.writes_enabled,
        "known_index": index,
        "usage_month": usage,
        "captures": captures,
        "cards": cards,
        "decisions": decisions,
        "alerts": alerts,
    }


def write_report(db: Database, user: Learner) -> Path:
    report = learner_report(db, user)
    day = datetime.now(ZoneInfo(user.timezone)).date().isoformat()
    folder = db.data_dir / "reports" / user.id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (day + ".md")
    lines = [
        f"# Wortling · {user.display_name} · {day}",
        "",
        f"Writes enabled: {user.writes_enabled}",
        f"Known-index entries: {report['known_index']['entries']}",
        f"Monthly model cost: ${report['usage_month']['cost_usd']:.4f}",
        "",
    ]
    if not report["known_index"]["entries"]:
        lines += [
            "Known decks are not indexed. Dedupe against existing Anki decks is unverified.",
            "",
        ]
    lines += ["## Cards", ""]
    for card in report["cards"]:
        fields = card["fields"]
        lines += [
            f"- {card['status']}: {fields['Lemma']} — {fields['Gloss']}",
            f"  {fields['Text']}",
        ]
    lines += ["", "## Decisions", ""]
    for decision in report["decisions"]:
        lines.append(
            f"- {decision['lemma'] or '(capture)'} · {decision['stage']} · "
            f"{decision['outcome']}: {decision['reason']}"
        )
    lines += ["", "## Alerts", ""]
    for alert in report["alerts"]:
        context = f" · capture {alert['capture_id']}" if alert["capture_id"] else ""
        lines.append(f"- {alert['kind']} · {alert['created_at']}{context}: {alert['message']}")
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path
