# Collection safety contract

1. Bootstrap an empty robot collection by login and full download only.
2. Never implement or invoke a full upload. A required full sync aborts with an alert.
3. Every write session: per-learner exclusive lock, backup, sync, read/write, sync.
4. Never delete notes or change existing scheduling except moving unseen new cards.
5. Update owned notes through stored note IDs; preserve card IDs and review history.
6. Keep the last seven pre-session collection backups.
7. An oversized write session adds zero notes unless explicitly forced after review.
8. All sync tests use a local test server and disposable collections.
9. Real AnkiWeb access requires explicit user confirmation. Writes additionally require
   human review of dry-run output. Neither is implied by deploying the capture API.
10. Credentials and capture tokens are supplied through the environment only.

The complete user plan lives in the initiating task. Milestones are recorded in
plan.md. Later milestones must not silently weaken these requirements.
