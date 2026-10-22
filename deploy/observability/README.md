# Noya private observability and remote diagnostics

**Architecture:** aiogram polling worker + independent Django ASGI operations
endpoint + independent diagnostics worker. All processes share a *separate*,
bounded SQLite operations store (not the production Django DB). OpenTelemetry
exports safe spans, metrics and structured logs to optional Grafana Alloy /
OpenObserve. No service requires a public diagnostic port.

## 1. Lightweight deployment

Install the existing application dependencies; the lightweight profile needs
no monitoring cluster. For the separate ASGI server, install the optional
`requirements-observability.txt` (includes Uvicorn and OTel/structlog).

Create the shared private state directory *before* starting services:

```bash
sudo install -d -o noya -g noya -m 700 /var/lib/noyaops
```

Configure the SAME values in the polling bot process, the API process and the
diagnostics-worker process. Use a secret manager or a root-readable EnvironmentFile:

```ini
NOYA_ENVIRONMENT=production
NOYA_SERVICE_NAME=noyabot
NOYA_OPS_STATE_DIR=/var/lib/noyaops
NOYA_OPS_PERSIST_LOGS=true
NOYA_OPS_LOG_RETENTION_DAYS=7
NOYA_OPS_LOG_MAX_ROWS=50000
NOYA_OPS_READ_TOKEN=<separate-48-byte-random-secret>
NOYA_OPS_RUN_TOKEN=<a-different-48-byte-random-secret>
NOYA_OPS_ALLOW_STAGING_TESTS=false
NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT=
```

Generate each secret locally with
`python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
**Do not commit or paste real tokens into logs/README.** Existing moderation
`StaffAPIKey` keys do NOT authenticate the operator API. Only superuser
sessions (with CSRF) and dedicated read/run tokens work. A read token cannot
enqueue diagnostics. Rotate by setting the optional `*_PREVIOUS` variable
during a brief transition, then remove it.

Run separate systemd units based on the two examples here, plus the existing
Telegram polling service. The polling service also needs
`NOYA_OPS_STATE_DIR=/var/lib/noyaops`. The diagnostics API must bind to
`127.0.0.1:8000` only. The worker must be running to process queued tests:

```bash
/opt/noyabot/.venv/bin/python manage.py run_noyaops_worker
```

The HTTP process never starts Django test suites, Telegram polling, shell
commands or subprocesses from user-supplied text.

## 2. Local access from Pop!_OS

```bash
ssh -N -L 8000:127.0.0.1:8000 user@server
export NOYAOPS_URL=http://127.0.0.1:8000/api/internal/ops/v1
export NOYAOPS_API_KEY='<ops read or run token>'
python -m noyaops status
python -m noyaops runtime
python -m noyaops logs --follow
python -m noyaops logs --level error --service noyabot
python -m noyaops metrics
python -m noyaops trace <32-hex-trace-id>
python -m noyaops tests list
```

To start a safe remote test, use the **run token**, not the read token:

```bash
python -m noyaops tests run process
# Receives a queued run ID. Poll or fetch its persisted result:
python -m noyaops tests result RUN_ID
python -m noyaops tests events RUN_ID
python -m noyaops tests report RUN_ID
```

The API endpoint `POST /api/internal/ops/v1/diagnostics/runs/` returns
HTTP 202; the dedicated worker picks up jobs asynchronously. Results survive
HTTP process restarts. A queue with no worker stays queued until it starts.

`GET /api/internal/ops/v1/logs/` uses bounded per-host persistent records
with `cursor`, `limit`, `since`/`until` (UTC Unix seconds), `level`,
`service`, `trace_id`, `correlation_id` filters. The API never returns raw
message bodies, provider prompts, reasoning, secrets, or unknown free-text log
messages. The `metrics/summary` endpoint is a **single HTTP process** snapshot;
the OTLP backend is the correct source for cross-process metrics.

## 3. Safe diagnostic groups

Production permits only predefined read-only checks:
`process`, `database`, `deployment`, `telegram_heartbeat`, `schema`,
`telemetry`, and `provider_tcp`. The provider check only opens a bounded TCP
connection to the server-configured `NOYA_API_URL`, **no paid AI request**.

Staging can additionally enable fixed test suites
(`regression_outputs`, `regression_security`, `regression_memory`) by setting:

```ini
NOYA_ENVIRONMENT=staging
NOYA_OPS_ALLOW_STAGING_TESTS=true
```

Those tests run as fixed Python argument vectors inside resource-limited
subprocesses with disposable SQLite Django test databases and cleared
provider/bot keys. API clients cannot supply file paths, shell commands,
Python source, test modules, URLs, or DB queries. Raw test output is not
persisted. The runner enforces a hard timeout. It needs enough RAM/CPU for
Django startup; do not run staging tests on a resource-starved production node.

## 4. Full profile (optional)

The included `docker-compose.observability.yml` runs OpenObserve and
Grafana Alloy; both interfaces map to **127.0.0.1 on the host**, not a public
interface. It is a separate heavyweight option, not suitable for a micro-VM.
Set appropriate disk and memory limits and keep OpenObserve on a separate
monitoring host when production resources are scarce. Review supplied container
image tags and updates before production deployment.

```bash
docker compose -f deploy/observability/docker-compose.observability.yml config
docker compose -f deploy/observability/docker-compose.observability.yml up -d
```

Configure the application for Alloy:

```ini
NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

Grafana Alloy receives OTLP/HTTP on 4318 and forwards OTLP traces/metrics/logs
with batching to OpenObserve organization `default`. The OTLP backend's UI
is available via SSH forwarding of port 5080. This repo includes six
`dashboards/noya-*.json` **Grafana-style templates**, which are not
automatically imported into OpenObserve; use the supported OpenObserve UI to
build equivalent dashboards where native JSON import is unavailable.

OTLP exports and file-backed diagnostics are fail-open for the Telegram worker.
Degraded monitoring must not interrupt messages. Trace/metric exporters
must not include user IDs as metric labels.

## 5. Validate before rollout

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python -m compileall botapp config noyaops
python manage.py test botapp.test_ops_api botapp.test_observability botapp.test_agent_output_paths
python manage.py test
```

Then validate the dedicated diagnostic worker with a queued `process`
test, CLI access through an SSH tunnel, bot heartbeat, and the monitoring
backend separately. No endpoint can deploy, restart the bot, mutate a
production database or execute caller-supplied commands. CI does not prove
live production connectivity.
