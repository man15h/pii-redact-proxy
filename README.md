# pii-redact-proxy

A redacting reverse proxy that sits between an AI agent and a model provider.
It replaces sensitive strings with stable placeholders on the way out and
restores them on the way back, so the provider never sees the real values and
the agent never sees a placeholder.

## Providers

One redaction core, one provider plugin per wire format:

| `PROVIDER` | Wire format | Default upstream |
|---|---|---|
| `anthropic` | Anthropic Messages API, streaming and not | `api.anthropic.com` |
| `grok` | OpenAI Chat Completions, with Grok Build's own headers forwarded | `api.x.ai` |
| `chatgpt` | OpenAI Responses API, for ChatGPT-account clients such as Codex CLI | `chatgpt.com` |

Providers differ in wire format and upstream, not in redaction. `grok` and
`chatgpt` refuse anything but GET on a path they have no handler for, so an
unknown endpoint can't carry unredacted content upstream.

```
pii_redact_proxy/
  core/        session, redactor, recognizers, JSON traversal, ops routes
  providers/   one module per provider, behind one protocol
  __main__.py  PROVIDER=<name> selects the plugin
tests/
  import_check.py     every import line in the package, presidio stubbed
  characterization/   golden fixtures and the recorder that produced them
```

## Running

```sh
docker run --rm -p 8082:8082 \
  -e PROVIDER=anthropic \
  -v ./config.yml:/app/config.yml:ro \
  ghcr.io/<owner>/pii-redact-proxy:<version>
```

Then point the client's base URL at the proxy, e.g.
`ANTHROPIC_BASE_URL=http://localhost:8082`.

| Variable | Default | Purpose |
|---|---|---|
| `PROVIDER` | (required) | Which provider plugin to serve |
| `PORT` / `HOST` | `8082` / `0.0.0.0` | Listen address |
| `UPSTREAM_BASE_URL` | the provider's own | Override the upstream |
| `LOG_LEVEL` | `INFO` | Python log level |
| `PII_REDACT_<LABEL>` | | Any value set this way is redacted as `<LABEL>` |

## config.yml

The redaction targets. It is not in the image: it lists exactly what is worth
protecting, so it belongs to the deployment and is mounted at
`/app/config.yml`. The proxy refuses to start without it.

```yaml
domains:
  - pattern: 'example\.com'       # regex, matched on word boundaries
    replacement_prefix: DOMAIN
hostnames: [db01, build-runner]   # literal names
usernames: [alice]
paths:
  - pattern: '/home/alice/\S*'    # regex
static_mappings:
  "Alice Example": "<OWNER_NAME>" # exact string -> fixed placeholder
```

Private IPv4 ranges, `ssh://` URLs and common API-token shapes (Anthropic,
OpenAI, GitHub, GitLab, AWS, bearer tokens) are recognised without any
config.

## Ops routes

| Route | Purpose |
|---|---|
| `GET /health` | Liveness, plus how many mappings are held |
| `GET /version` | The provider's behaviour version and the `BUILD_SHA` baked into the image |
| `GET /mappings` | Every placeholder held, with only the first 4 characters and the length of each original |
| `POST /mappings/clear` | Drop all mappings |

These routes are unauthenticated. Don't expose the proxy beyond the clients
that use it.

## Tests

```sh
pip install fastapi httpx pyyaml uvicorn
python tests/import_check.py
cd tests/characterization && python characterize.py verify --app-path ../..
```

Neither needs Presidio or spaCy, so both run in seconds. The fixtures are
recorded wire behaviour, and every change has to reproduce them byte for
byte; `tests/characterization/README.md` covers recording new ones.

## Build and publish

Images are built only when a change lands on `main`. Pull requests run the
tests but never build or push, and there is no manual trigger. Each merge
that touches the package, the Dockerfile or `requirements.txt` makes
`.github/workflows/build.yml`:

1. Run the import check and the fixtures.
2. Bump the patch version from the highest `vX.Y.Z` tag. The first release
   is `v0.1.0`.
3. Build `linux/amd64` and push `ghcr.io/<owner>/pii-redact-proxy` as
   `<version>`, `latest` and `sha-<commit>`, with the commit stamped into
   `/version`.
4. Push the `v<version>` git tag.

The run's job summary prints the reference to pin:
`ghcr.io/<owner>/pii-redact-proxy:<version>@sha256:<digest>`.
