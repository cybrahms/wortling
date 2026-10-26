# Wortling

Capture German text, web pages, and photos, then turn selected encounters into Anki cards.
Each learner has a separate queue, known-word index, and collection.

The [card style guide](docs/card-style.md) defines context selection, short-label
cards, answer layout and the always-visible full translation. Useful vocabulary
must not be discarded merely because it came from a label or interface.

The GitHub repository is a read-only mirror. Changes are made in the primary
Gitea repository and published automatically.

## Local development

```sh
cp config.example.yaml config.yaml
uv sync --frozen
uv run playwright install firefox
uv run wortling config-check
uv run wortling init
uv run wortling serve
```

Set the capture-token environment variables listed in the configuration to random
values of at least 32 characters. The API listens on localhost:8787. Processing,
real AnkiWeb access, and writes are disabled by default. Configure your own learner
profiles, model provider credentials, and collection mappings before enabling them.

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

## Deployment

Create an ignored `.env` and `config.yaml`, then build with `scripts/build.sh` and
start the service with `docker compose up -d`. The script fetches `main` and refuses
a commit `main` does not contain, then builds with the image labelled with that
commit; Compose refuses a build that has no `BUILD_SHA`, so there is no path to an
unlabelled or unmerged image. The API and worker share a persistent data volume.
Configure HTTPS using your own
hostname and certificate; `deploy/Caddyfile.tailnet` is an environment-based example.
Keep the service behind a private network unless you have reviewed its exposure.
The example hostname `wortling.example.invalid` must be replaced for deployment.

Create a personal browser setup link using your actual HTTPS origin:

```sh
docker compose exec -T worker wortling setup-link --user learner --base-url https://YOUR_HOST
```

Links expire after 24 hours and work once. Share each link only with its learner.
See [operations](docs/operations.md), [sync safety](docs/safety.md), and
[iPhone sharing](docs/shortcut.md). Signed Shortcuts are generated locally and
excluded from version control. The repository includes only a placeholder source
and an inspectable unsigned template.

No project license has been selected yet.
