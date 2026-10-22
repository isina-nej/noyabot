# Noya observability profiles

Noya supports a lightweight profile for a small application host and a full
profile for a separate monitoring host. Both keep the operations API private;
the bot continues to work when telemetry export is unavailable.

## Lightweight profile

The application keeps a bounded, redacted in-process log ring and exposes the
authenticated Django operations API. Bind Django to localhost and use an SSH
forward (or a private Tailscale address):

```bash
ssh -N -L 8000:127.0.0.1:8000 user@server
export NOYAOPS_URL=http://127.0.0.1:8000/api/internal/ops/v1
export NOYAOPS_API_KEY='read-from-your-secret-manager'
python -m noyaops doctor
```

The CLI also reads `~/.config/noyaops/config.toml`. Keep that file mode `600`:

```toml
url = "http://127.0.0.1:8000/api/internal/ops/v1"
api_key = "replace-me"
timeout = 8
retries = 2
retry_backoff = 0.5
poll_interval = 2
```

Environment variables override the file, and command-line options override
both. Never put an API key in a URL or commit it to a repository.

Useful commands:

```text
python -m noyaops status
python -m noyaops health
python -m noyaops runtime
python -m noyaops logs --level error --follow
python -m noyaops logs --service botapp --trace-id TRACE_ID
python -m noyaops trace TRACE_ID
python -m noyaops metrics
python -m noyaops tests list
python -m noyaops tests run process
python -m noyaops tests result RUN_ID
python -m noyaops doctor --json
```

`--json` emits machine-readable output. Read operations retry bounded
transient failures; diagnostic POSTs are never retried to avoid duplicate
runs. Every request includes an `X-Request-ID` correlation header.

## Full profile (Alloy + OpenObserve)

The compose file is deliberately opt-in and binds every host port to
localhost. Review image versions, credentials, disk capacity, and private
networking before production use:

```bash
export OPENOBSERVE_ADMIN_EMAIL=ops@example.invalid
export OPENOBSERVE_ADMIN_PASSWORD='use-a-secret-manager'
export OPENOBSERVE_DATA_RETENTION_DAYS=7
export OPENOBSERVE_MAX_FILE_SIZE_IN_MEMORY_MB=128
docker compose --profile full \
  -f deploy/observability/docker-compose.observability.yml up -d
```

The two admin variables are consumed as container environment values by this
example; use an external secret store or Docker secrets in production and do
not paste the rendered `docker compose config` output into logs.

Alloy receives OTLP/HTTP on `127.0.0.1:4318` and forwards logs, metrics, and
traces to OpenObserve. Set the application's
`NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT` only after Alloy is reachable. The
compose example caps Alloy at 256 MiB/0.5 CPU and OpenObserve at 512 MiB/1 CPU,
keeps container logs bounded, persists OpenObserve data, and applies the
configured retention days. A container healthcheck is included for the
OpenObserve `/healthz` endpoint; verify the image's bundled `wget` when
upgrading the image. The retention variable maps to OpenObserve's
`ZO_COMPACT_DATA_RETENTION_DAYS`; increase resource limits only after
measuring the host. Alloy also drops telemetry above its 180 MiB hard (144 MiB
soft) memory budget and keeps a 1,000-batch in-memory export queue. OpenObserve
requires a retention value of at least three days.

The JSON files under `dashboards/` are dashboard definitions for the five
operator views. OpenObserve's dashboard import format can change between image
versions, so import them through the private UI/API after validating the
target version; no public dashboard endpoint is exposed by this example.
For component and retention changes, use the [Alloy OTLP HTTP exporter
reference](https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.exporter.otlphttp/)
and [OpenObserve environment variable
reference](https://openobserve.ai/docs/administration/configuration/environment-variables/).

## Internal API service with systemd

On a host that runs the Django application directly, copy
`noyabot-observability.service.example` to `/etc/systemd/system/`, review the
user, working directory, environment file, and resource limits, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now noyabot-observability.service
systemctl status noyabot-observability.service
```

The example binds Gunicorn to `127.0.0.1:8000`; expose it through SSH or a
private network only.

## Security and boundaries

The API rejects shell commands, file paths, prompts, arbitrary test modules,
and arbitrary code. Diagnostic runs use only the pre-registered checks shown
by `python -m noyaops tests list`. Keep the API behind SSH/Tailscale and use a
scoped, rotatable `X-API-Key`; the collector and OpenObserve ports are also
localhost-only by default.
