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

For Synthetic, add `SYNTHETIC_API_KEY` to the private `.env` and configure:

```yaml
llm:
  base_url: https://api.synthetic.new/openai/v1
  api_key_env: SYNTHETIC_API_KEY
  reasoning_effort: none
  models:
    normalize: syn:small:vision
    extract: syn:large:text
    judge: syn:large:text
    generate: syn:large:text
  cost_per_million:
    syn:small:vision: [0.6, 1.2]
    syn:small:text: [0.1, 0.5]
    syn:large:text: [0.6, 1.2]
```

Set `reasoning_effort` explicitly for reasoning-capable models: hidden reasoning
shares the output token budget with the required JSON. Omit the setting for
providers that do not support it. A `finish_reason: length` response is reported
as `output_truncated`, including token usage, without a redundant schema retry.
A provider switch must pass real inference and semantic validation on a disposable
copy of a capture before deployment; `/models` alone cannot establish compatibility.

An explicit `api_key_env` requires that credential and never falls back to Hyper.
The normalize stage needs a vision model for image captures. Synthetic aliases
follow the provider's current models; validate structured output after changes.
These price estimates were read from Synthetic's `/models` response on 2026-10-17;
refresh them when changing models. Account billing is authoritative when the
provider does not return a cost in its usage metadata.

## Operational monitoring

`/v1/health` is API/SQLite liveness only. It does not establish that captures can
be processed. Run `wortling ops-check --probe-provider` in the worker for a JSON
snapshot and a nonzero exit on degradation. It reports aggregate counts and ages,
without source text, credentials, or provider response bodies.

The worker updates a heartbeat before and after each loop. The operational check
flags a missing heartbeat or one older than 15 minutes, pending work more than
15 minutes past its next attempt, failed jobs, expired running leases, all
unacknowledged application alerts, missing/successful backups older than 30 hours,
and collection syncs more than 30 minutes past their scheduled time. Pending work
for a disabled learner is still reported: accepting captures without processing
them must be visible. Provider probing authenticates `/models` and checks that
all configured models exist. It does not spend inference tokens or prove that
inference quotas and structured generation work; actual failures become queue
and application-alert signals.

On this macOS station, run `python3 deploy/install-monitor.py` to install an
independent launchd check every five minutes. `scripts/monitor.py` checks API
liveness and invokes the worker's operational check with a 60-second deadline.
A stopped container, unavailable Docker, or timed-out probe therefore alerts
even when the application cannot report its own health. The host and its logged-in
launchd session must be running for this check to run.

The watchdog uses the station's existing `~/.config/iris-telegram/{token,chat_id}`
files, pages on new issues, repeats after an hour, and sends a recovery message.
Failed delivery does not advance the notification state. Inspect
`~/Library/Logs/wortling/monitor.log` for every snapshot and
`~/.local/state/wortling-monitor/state.json` for the last delivery state.
`python3 scripts/monitor.py --dry-run` checks without sending or changing state.
Do not acknowledge application alerts until their underlying condition is resolved.

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
