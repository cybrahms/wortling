# Wortling

Capture German text, web pages, and photos, then turn selected encounters into Anki cards.
Each learner has a separate queue, known-word index, and collection.

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

Create an ignored `.env` and `config.yaml`, then run `docker-compose up --build -d`.
The API and worker share a persistent data volume.

On macOS the `docker compose` plugin is often not installed while the v1 binary is
(`/opt/homebrew/bin/docker-compose`, which has no `compose` subcommand), so the
hyphenated form is the one that works. Both are the same Compose file. Configure HTTPS using your own
hostname and certificate; `deploy/Caddyfile.tailnet` is an environment-based example.
Keep the service behind a private network unless you have reviewed its exposure.
The example hostname `wortling.example.invalid` must be replaced for deployment.

Create a personal browser setup link using your actual HTTPS origin:

```sh
docker-compose exec -T worker wortling setup-link --user learner --base-url https://YOUR_HOST
```

Links expire after 24 hours and work once. Share each link only with its learner.
See [operations](docs/operations.md), [sync safety](docs/safety.md), and
[iPhone sharing](docs/shortcut.md). Signed Shortcuts are generated locally and
excluded from version control. The repository includes only a placeholder source
and an inspectable unsigned template.

No project license has been selected yet.
