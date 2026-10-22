# Noya AI Ops: server-side evidence → tests → proposed code repair

This feature complements the existing lightweight Noya Ops API. **The AI runs
on the Ubuntu server, not the operator's laptop.** It is an opt-in coding
agent, not an unrestricted remote shell or an automatic production deploy.

## Preconditions

- `noyaops-api.service` and `noyaops-worker.service` are running on the
  server, with the private read/run tokens in a root-only environment file.
- A successful remote `process` diagnostic has already been verified.
- The production checkout `/opt/noyabot` may contain many local changes:
  **never** run `git reset --hard`, `git clean -fd`, or Codex in that path.
- The AI agent must **not run as root**. It receives a scrubbed snapshot created
  by the operator, not the Ops token, raw Telegram journal, or production
  `.env`. It works in a separate fresh Git clone.
- A private repository needs a separately scoped read-only GitHub credential
  for the cloning phase; don't give the agent a production GitHub write token.
- Codex CLI installed and signed in under the agent's own account using an
  supported authentication mode. The Codex service may incur usage costs.

## 0. Fetch the AI Ops code without touching the running bot

As root on the server:

```bash
git -C /opt/noyabot fetch origin feat/aiops-server-repair-runner
git -C /opt/noyabot worktree add --detach /opt/noyabot-aiops-review \\
  origin/feat/aiops-server-repair-runner
cd /opt/noyabot-aiops-review
/opt/noyabot/.venv/bin/python -m noyaops.aiops --help
```

This creates an **additional** clean review worktree. It does not update
`/opt/noyabot`, restart Telegram or alter the already-running Ops services.

## 1. Prepare a restricted agent identity

Run **on the server**, as root. This user has no sudo privileges or group
membership in `systemd-journal`, `docker`, or the production service:

```bash
id noya-aiops || useradd --system --create-home \
  --home-dir /var/lib/noya-aiops --shell /usr/sbin/nologin noya-aiops

install -d -o noya-aiops -g noya-aiops -m 700 /var/lib/noya-aiops
install -d -o noya-aiops -g noya-aiops -m 700 /var/lib/noya-aiops/workspaces
install -d -o root -g noya-aiops -m 750 /var/lib/noya-aiops/input
```

Ensure the production `/opt/noyabot/.env`, API secrets, systemd environment
files and SSH keys are unreadable by `noya-aiops` before running an AI agent.

## 2. Collect a safe evidence snapshot as the trusted operator

Use the **new AI Ops review checkout** `/opt/noyabot-aiops-review`;
 do not change the active bot:

```bash
cd /opt/noyabot-aiops-review
set -a
source /root/noyaops-review.env
set +a
export NOYAOPS_API_KEY="$NOYA_OPS_READ_TOKEN"

umask 077
/opt/noyabot/.venv/bin/python -m noyaops.aiops snapshot \
  > /var/lib/noya-aiops/input/latest-snapshot.json
chgrp noya-aiops /var/lib/noya-aiops/input/latest-snapshot.json
chmod 640 /var/lib/noya-aiops/input/latest-snapshot.json
```

Read the report before using it with an external coding model. It includes:

- active `tinkera-robot` service status;
- the actual **on-disk** checkout Git SHA and dirty-file counts;
- Ops review SHA and reachable remote `main`/feature HEAD SHAs;
- Ops API health, bot/worker heartbeats, bounded **redacted** events and
  per-HTTP-process metrics;
- explicit gaps such as `real_telegram_bot_not_emitting_ops_heartbeat`.

It **cannot** prove the exact loaded Python bytecode SHA from a running process
and does not falsely claim that an old dirty production directory matches
GitHub HEAD. No raw journal text, user messages, prompts, tokens or patches
are included.

## 3. Install Codex CLI for the restricted agent

Install Codex **under the new user**, following the current official installation
guide: https://learn.chatgpt.com/docs/codex/cli. The official Linux installer is:

```bash
runuser -u noya-aiops -- env \
  HOME=/var/lib/noya-aiops \
  sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
```

Before running scripts from the internet on a production server, review their
source and obtain it from the official publisher. Sign in via a device code:

```bash
runuser -u noya-aiops -- env \
  HOME=/var/lib/noya-aiops \
  /var/lib/noya-aiops/.local/bin/codex login --device-auth
```

Open the shown sign-in URL on any device. Only the **authenticated agent
process** runs on the server. The operator's phone/browser doesn't run code.

## 4. Diagnose with AI (read-only) / propose code edits

Run under the unprivileged agent user with a fresh clean environment. No ops
credentials or production API keys are inherited by the agent:

```bash
cd /opt/noyabot-aiops-review
runuser -u noya-aiops -- env -i \
  HOME=/var/lib/noya-aiops USER=noya-aiops LOGNAME=noya-aiops \
  PATH=/var/lib/noya-aiops/.local/bin:/usr/local/bin:/usr/bin:/bin \
  PYTHONPATH=/opt/noyabot-ops-review \
  /opt/noyabot/.venv/bin/python -m noyaops.aiops diagnose \
  "Determine the root cause of the observed Noya Agent/API errors" \
  --snapshot-file /var/lib/noya-aiops/input/latest-snapshot.json

# Narrowly propose a fix (modifies *only* a new clean checkout):
runuser -u noya-aiops -- env -i \
  HOME=/var/lib/noya-aiops USER=noya-aiops LOGNAME=noya-aiops \
  PATH=/var/lib/noya-aiops/.local/bin:/usr/local/bin:/usr/bin:/bin \
  PYTHONPATH=/opt/noyabot-ops-review \
  /opt/noyabot/.venv/bin/python -m noyaops.aiops repair \
  "Fix the reported failing Agent regression and add focused coverage" \
  --snapshot-file /var/lib/noya-aiops/input/latest-snapshot.json
```

The output includes the **new isolated workspace path**, base SHA, changed
paths, and test statuses. Review changes there:

```bash
git -C /var/lib/noya-aiops/workspaces/repair-XXXX status --short
git -C /var/lib/noya-aiops/workspaces/repair-XXXX diff --check
git -C /var/lib/noya-aiops/workspaces/repair-XXXX diff
```

No PR is automatically opened, no write credential is passed to Codex and
nothing touches Production. To contribute the fix, review and push its branch
through the normal protected GitHub PR/CI process using an operator identity.
Do not deploy until clean CI and a staged acceptance test have passed.

## 5. Test a real configured server dependency

For a fixed no-side-effect `provider_tcp` test, execute with the **run** token
as the operator (not the AI coding user):

```bash
set -a; source /root/noyaops-review.env; set +a
/opt/noyabot/.venv/bin/python -m noyaops.aiops test provider_tcp
```

This confirms TCP reachability to the **server-configured** provider endpoint;
it does NOT prove model quality or the full Telegram-to-9Router chain.

## Gaps still to implement before claiming comprehensive remote AI debugging

1. **Instrument the live bot:** merge and deploy the Ops hooks into the active
   `/opt/noyabot` **without losing local modifications**, then share
   `NOYA_OPS_STATE_DIR` to emit real bot heartbeats and sanitized events.
2. **Live synthetic chat test:** add a separate no-Telegram-send staging runner
   with its own 9Router budget/credentials, tool mocking and safety gates.
3. **Full end-to-end production coverage:** verify 9Router response parsing,
   tool executions, media, memory, permissions, output sanitization and
   Telegram send success with correlation IDs.
4. **Continuous repairs/CI:** use GitHub CI and explicit review; never give a
   public self-hosted GitHub Actions runner on the same production box access
   to production secrets. See https://docs.github.com/en/actions/reference/security/secure-use.

Nothing in this package creates a remote shell API. Never expose coding
commands on a public endpoint or let model text choose subprocess argv.
