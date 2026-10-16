import argparse
import json

from wortling.config import load_settings
from wortling.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="wortling")
    parser.add_argument("--config")
    parser.add_argument(
        "command",
        choices=[
            "config-check",
            "init",
            "serve",
            "worker",
            "bootstrap",
            "sync",
            "index",
            "known",
            "report",
            "reprocess",
            "write",
            "backup",
            "backup-verify",
            "setup-link",
        ],
    )
    parser.add_argument("argument", nargs="?")
    parser.add_argument("--user", default="learner")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--since", default="v1")
    parser.add_argument("--capture-id", help="Reprocess just this learner's capture")
    parser.add_argument(
        "--explicit-lookup", action="store_true", help="Learner requested the capture's vocabulary"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--base-url", default="https://wortling.example.invalid")
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.command == "config-check":
        print(f"Valid configuration: {len(settings.users)} learners")
    elif args.command == "init":
        Database(settings.data_dir).initialize(settings)
        print("Database initialized")
    elif args.command == "setup-link":
        from wortling.browser_auth import issue_setup_link

        settings.learner(args.user)
        db = Database(settings.data_dir)
        db.initialize(settings)
        print(issue_setup_link(db, args.user, args.base_url))
    elif args.command == "serve":
        import uvicorn

        from wortling.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
    elif args.command in {"report", "reprocess", "write", "backup", "backup-verify"}:
        from pathlib import Path

        from wortling.anki.writer import write_session
        from wortling.backup import backup_all, verify_backup
        from wortling.report import write_report
        from wortling.reprocess import reprocess

        db = Database(settings.data_dir)
        db.initialize(settings)
        user = settings.learner(args.user)
        if args.command == "report":
            print(write_report(db, user))
        elif args.command == "reprocess":
            count = reprocess(
                db,
                user.id,
                args.since,
                capture_id=args.capture_id,
                explicit_lookup=args.explicit_lookup,
            )
            print(f"Queued {count} captures")
        elif args.command == "write":
            print(json.dumps(write_session(settings, db, user, force=args.force)))
        elif args.command == "backup":
            print(backup_all(settings, db))
        else:
            if not args.argument:
                parser.error("backup-verify requires a backup directory")
            print(f"Verified {verify_backup(Path(args.argument))} files")
    elif args.command in {"bootstrap", "sync", "index", "known"}:
        from wortling.anki.reader import (
            effective_status,
            inspect_index,
            open_collection,
            rebuild_index,
        )
        from wortling.anki.sync import bootstrap_download, incremental_sync, login
        from wortling.lemma import lemma_key

        db = Database(settings.data_dir)
        db.initialize(settings)
        user = settings.learner(args.user)
        if args.command == "known":
            if not args.argument:
                parser.error("known requires a word")
            with db.connect() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM known_index WHERE user_id=? AND lemma_key=?",
                        (user.id, lemma_key(args.argument)),
                    )
                ]
            print(
                json.dumps(
                    {"status": effective_status(rows) or "absent", "matches": rows},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            with open_collection(
                settings.data_dir, user.id, create=args.command == "bootstrap"
            ) as collection:
                if args.command in {"bootstrap", "sync"}:
                    auth = login(collection, user, settings.anki_sync_endpoint)
                    if args.command == "bootstrap":
                        bootstrap_download(collection, auth)
                    else:
                        incremental_sync(collection, auth)
                    print(f"Indexed {rebuild_index(db, user, collection)} entries")
                elif args.argument == "inspect":
                    print(json.dumps(inspect_index(collection), ensure_ascii=False, indent=2))
                else:
                    print(f"Indexed {rebuild_index(db, user, collection)} entries")
    else:
        from wortling.worker import run

        run(settings, once=args.once)
