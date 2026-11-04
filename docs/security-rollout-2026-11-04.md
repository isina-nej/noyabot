# NoyaBot security rollout and incident response (2026-11-04)

These steps deliberately **do not** delete live Telegram sessions or force
a deployment. The repository previously tracked `noyabot.session` and
`x-bridge/cookies.json`; treat copies in Git history as compromised until
both providers revoke the corresponding sessions.

## Step 0: independent backup and service audit

- Confirm the server's deployment commit, active processes and application DB
  before editing credentials. Back up the database using its native consistent
  backup mechanism (SQLite's backup API or MySQL's supported dump/snapshot).
- List tracked secret paths without printing their contents:
  `git ls-files -- noyabot.session x-bridge/cookies.json`.
- Protect backups and any new credential paths with restrictive filesystem
  permissions and separate ownership. Never paste session bytes into logs.

## Step 1: rotate credentials at the source

1. In Telegram **Settings > Devices**, revoke the exposed Telethon
   authorization and authenticate a fresh, separate session.
   Temporarily set `CHANNEL_ANALYSIS_ENABLED=false` while doing this if the
   Telethon session is still in use.
2. In X/Twitter, terminate the affected session, create an independent
   authorized session and place the new `cookies.json` outside the checkout
   (for example `/var/lib/noya-secrets/x-cookies.json`).
3. Set `X_BRIDGE_COOKIES_FILE` to that absolute path and set a new random
   `X_BRIDGE_INTERNAL_TOKEN`; all bridge requests now require a bearer token.
   Without it, the Bridge responds 401 rather than silently allowing access.
4. Set `TELETHON_SESSION_PATH` to the fresh external session path. Rotate any
   exposed BotFather/API credentials as appropriate; avoid putting secrets
   in Git, command-line arguments or debug output.

## Step 2: harden Django and sandbox

Set these deployment settings **before restarting the app**:

```dotenv
NOYA_ENVIRONMENT=production
DEBUG=false
DJANGO_SECRET_KEY=<random secret generated securely; at least 32 characters>
ALLOWED_HOSTS=your.actual.domain
NOYA_SANDBOX_ENABLED=false
X_BRIDGE_COOKIES_FILE=/var/lib/noya-secrets/x-cookies.json
X_BRIDGE_INTERNAL_TOKEN=<fresh private random secret>
```

Use real values; never commit them. The hardened settings will reject
insecure production `DEBUG`, wildcard hosts and weak/placeholder Django
keys. The sandbox now uses a killable subprocess for development and testing,
but its opt-in execution is **not** a substitute for a network/FS-isolated
container. Production should leave it disabled.

Before deployment, install patched dependencies:

```bash
python -m pip install -r requirements.txt
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test botapp.test_security_runtime_hardening
```

Back up the DB before every migration. Keep the currently running service
unchanged if a check fails.

## Step 3: remove the already-invalid sessions from Git

Only after confirming **new** credentials are active on all deployed
services:

```bash
git rm --cached -- noyabot.session x-bridge/cookies.json
git commit -m "security: stop tracking revoked session files"
```

This removes the files from future commits, **not** the old commits. To
rewrite published history, coordinate a maintenance window for clones,
PRs and deployments, work on a fresh bare clone (not the production
working tree), and use `git-filter-repo` to remove these paths across
the entire history. Rewriting Git history requires a carefully coordinated
force push, re-cloning downstream copies, and refreshing any affected CI
references. Provider-side session revocation is mandatory regardless of
Git history cleanup. Forks/caches may retain old snapshots.

## Step 4: verify safety

- Run CI (SQLite unit suite, MySQL lock regression, Ruff and pip-audit).
- Test explicit admin commands and confirmations with a protected target,
  including simulated Telegram lookup failures.
- Exercise Telegram / 9Router on a staging bot before production.
- Confirm live logs contain timing/event data, not raw user prompts/tool
  arguments; rotate or expire old logs following your retention policy.
- Confirm X Bridge returns 401 without a valid `Authorization: Bearer` header.
- Test bot session rotation, media, conversation and channel analytics.
- Roll out by staged restart with health checks and retained rollback
  artifacts. Do not automatically rerun ambiguous Telegram side effects.

## Remaining structural work

The security changes do not magically migrate existing
`MemoryConversation` ownership between two bot identities, turn process-local
state into distributed state, or eliminate every legacy agent/harness module.
Those changes require schema migration, a stated historical memory ownership
policy, multi-worker soak tests and a controlled deployment. Never
silently relabel legacy users' memories as belonging to a different bot.
