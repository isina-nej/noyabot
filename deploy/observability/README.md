# Noya observability profiles

The lightweight profile is the default.  It keeps a bounded, redacted in-process
log ring and exposes the authenticated Django operations API.  Reach it through
an SSH local forward or a private Tailscale network:

```text
ssh -N -L 8000:127.0.0.1:8000 user@server
NOYAOPS_URL=http://127.0.0.1:8000/api/internal/ops/v1 \
  NOYAOPS_API_KEY='...' python -m noyaops status
```

The full profile runs Grafana Alloy and OpenObserve on a private interface:

```bash
export OPENOBSERVE_ADMIN_EMAIL=ops@example.invalid
export OPENOBSERVE_ADMIN_PASSWORD='use-a-secret-manager'
docker compose -f deploy/observability/docker-compose.observability.yml up -d
```

Set `NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT=http://127.0.0.1:4318/v1/traces`
only after the collector is reachable.  Export failures are fail-open and do
not interrupt Telegram processing.  The compose file is an example; review
image versions and resource limits before production use.

The API never accepts shell commands, file paths, prompts, or arbitrary test
modules.  Its diagnostic runner executes only the pre-registered read-only
checks shown by `python -m noyaops tests list`.
