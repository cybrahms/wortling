# Operations

Keep `config.yaml`, `.env`, OAuth state, credentials, and `data/` private and
outside version control. Configure each learner using environment-variable names;
never put credential values in YAML, documentation, issues, or shared Shortcuts.

## Configuration and access

The sample configuration uses synthetic learners. Set your own IDs, display names,
timezone, CEFR profile, and known-deck field mappings. The defaults disable
processing and writes and point Anki sync at a local test server.

Run `wortling setup-link --user learner --base-url https://YOUR_HOST` inside the
worker container to issue a browser link. It expires after 24 hours and is consumed
by the Continue button. Browser sessions use a Secure, HttpOnly, SameSite=Strict
cookie. Rotate capture tokens and initialize/restart the service to revoke existing
tokens and sessions. Recreate Compose containers when changing environment files.

Set `WORTLING_BASE_URL` in the private environment to avoid repeating `--base-url`.
Commands default to the first learner in your configuration; pass `--user` to
select another learner. Neither the deployment origin nor learner IDs need to be
hardcoded in the source.

## Model provider

Configure `HYPER_API_KEY`, or supply a private OAuth environment file through
`WORTLING_HYPER_ENV_FILE`. The worker refreshes OAuth credentials under a process
lock. Keep this file writable only by the service account. Configure model names,
prompt versions, and budget limits for your own provider account.

## Collections and writes

Obtain the learner's permission before using real AnkiWeb credentials. Set
`real_ankiweb_approved: true`, provide their credential environment variables,
and set `anki_sync_endpoint: null` only for an authorized real account.

```sh
wortling bootstrap --user learner
wortling index inspect --user learner
wortling index --user learner
wortling known 'die Sitzung' --user learner
wortling report --user learner
```

Configure known-deck lemma fields from inspection; arbitrary numeric fields are
not frequency ranks. Review dry-run output before setting `processing_enabled`,
`dry_run_approved`, and `writes_enabled` for the learner. Set `promote_new_cards:
false` for an independent mined deck when existing new-card ordering must remain
untouched. Configure daily pacing in Anki.

```sh
wortling write --user learner
wortling reprocess --user learner --capture-id CAPTURE_ID --explicit-lookup
```

Full-sync-required aborts; there is no full-upload recovery path. Investigate the
schema change instead of forcing an upload. Reprocessing retains immutable raw
captures and updates only owned fields. See [the safety contract](safety.md).

## Backup and deployment

```sh
wortling backup
wortling backup-verify /data/backups/nightly/BACKUP_ID
```

Snapshots include SQLite, collections, media, and referenced capture images, with
integrity checks and a SHA-256 manifest. Credentials are excluded; snapshots still
contain personal capture and collection data and must remain private. Configure
`RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, and storage credentials separately for
encrypted off-host backup. Restore with API/worker stopped and writes disabled.
Never replace an open collection or force-upload a restored collection.

`scripts/deploy.sh` is an optional Gitea deployment helper requiring the separate
`forge` CLI, a clean pushed commit, successful CI, and no running capture jobs.
It is not required to run Wortling with Compose. Caddy configuration uses
`WORTLING_HOST`, `WORTLING_TLS_CERT`, and `WORTLING_TLS_KEY`; configure your private
network/proxy separately. Verify `/v1/health` after deployment.

## Verification

`uv run pytest -q` uses temporary data and local disposable sync servers, without
real AnkiWeb access. The 40 golden examples are synthetic seed sentences with
recorded model outputs. They do not establish real-world capture accuracy.
Regenerate deliberately with `scripts/record_golden.py` and review changed outputs.
Native iOS checks require macOS/Xcode; see [the harness](../scripts/ios/README.md).
Signed templates are built locally; see [iPhone sharing](shortcut.md).
