# NoyaBot Telegram Web for Bots + database inbox

## What is integrated

This solution **uses the real, pre-existing** [ohld/telegram-web-for-bot](https://github.com/ohld/telegram-web-for-bot)
(GPL-3.0-or-later) for the Telegram Web-style MTProto client. The upstream
source is independently cloned and built at the pinned SHA in
`upstream-revision.txt`. We do **not** copy its thousands of files into
NoyaBot, modify its license, or assume the frontend can connect to MySQL.

In parallel, NoyaBot now has a **separate, staff-authenticated database inbox**
at `/inbox/`, with a searchable sidebar, chat list, paginated messages, and an
optional send-as-bot action. The Django page links to the self-hosted upstream
interface. These are **two interfaces**, not yet a single merged React sidebar.
This avoids misrepresenting local MTProto caches as full server history.

### Why the two views are necessary

Upstream's own `docs/bot-mtproto-overrides.md` confirms that
`messages.getDialogs` / `messages.getHistory` return `BOT_METHOD_INVALID`
in bot mode. Its chat list comes from a bounded
`updates.getDifference` cache stored locally in the browser. It is NOT
the entire Telegram history.

Telegram's Bot API similarly cannot enumerate all existing chats or fetch
arbitrary historical messages. NoyaBot's original `MessageSnapshot` archived
only groups and did not record which of the two bots observed each message.
That model must not be reused to display private messages across two bot
identities.

The new `BotInboxMessage` model separately keys each event by
`(bot_id,chat_id,message_id)` and is fed by the **existing** aiogram
dispatcher, without any second Bot API long-poller.

## Source setup: real Telegram Web for Bots

Requirements: Node 22 / npm 10+, and Telegram API ID/hash
from https://my.telegram.org . The source is quite large and requires substantial
build space. **For low-memory/low-disk servers, build on a workstation/CI and
deploy the resulting static dist or Docker image rather than building on the
running 3.8 GiB production bot host.**

```bash
# From /opt/noyabot after checkout
bash deploy/telegram-web-for-bot/prepare-upstream.sh /opt/telegram-web-for-bot
cd /opt/telegram-web-for-bot
cp .env.example .env
# Edit .env to set TELEGRAM_API_ID, TELEGRAM_API_HASH and
# BASE_URL=https://botweb.example.org/
npm ci
npm run check:ts
npm run build:production
```

Run the built site through a private HTTPS reverse proxy or use the
`deploy/telegram-web-for-bot/compose.yml` with a separate .env file
containing `WEBBOT_SOURCE=/opt/telegram-web-for-bot`,
`BOT_WEB_URL=https://botweb.example.org/`, API ID and API hash.
The Compose port is **127.0.0.1:8431** only. Add TLS and
HTTP Basic Auth/restricted network access BEFORE exposing it.

**Do not publish your live BotFather token in frontend source, GitHub, logs,
Docker Compose, environment build args or screenshots.** The upstream's bot
login is a user interaction inside your self-hosted browser app; it stores
the bot token/session locally for Bot API fallbacks. Use a dedicated browser
profile, protect its origin and treat browser account storage as sensitive.

The pinned source is GPL-3.0-or-later. Preserve its copyright/license and
follow the license terms if distributing a modified build.

## Enable NoyaBot DB inbox

1. Back up the real database (MySQL if that's the configured backend).
2. Update NoyaBot, install requirements and run migrations, including 0037:
   ```bash
   cd /opt/noyabot
   git pull --ff-only origin main
   .venv/bin/python manage.py migrate --plan
   .venv/bin/python manage.py migrate
   .venv/bin/python manage.py check
   .venv/bin/python manage.py test botapp.test_bot_inbox
   ```
3. In the private `/opt/noyabot/.env` set:
   ```dotenv
   BOT_INBOX_ENABLED=true
   BOT_INBOX_SEND_ENABLED=false
   BOT_INBOX_RETENTION_DAYS=30
   BOT_WEB_URL=https://botweb.example.org/
   ALLOWED_HOSTS=inbox.example.org,localhost,127.0.0.1
   ```
4. Restart **only after migrations and checks**:
   `sudo systemctl restart tinkera-robot`.
5. Run a separate WSGI server (the aiogram Polling service is NOT a web server).
   Install `gunicorn` in the virtualenv if needed:
   ```bash
   .venv/bin/python -m pip install 'gunicorn>=23,<25'
   .venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8085 --workers 2 --threads 4
   ```
   Use systemd to keep it running in production. Never expose port 8085
   publicly; proxy it under **https://inbox.example.org** with TLS and
   extra HTTP Basic Auth (see `nginx-example.conf`).
6. Create a Django **staff** login if necessary:
   `.venv/bin/python manage.py createsuperuser`. Only authorized staff
   sessions can see the inbox or send; all endpoints are CSRF protected.
   Log in at `https://inbox.example.org/admin/login/?next=/inbox/`.
7. Enable outgoing text sending **only** after verifying operator permissions:
   ```dotenv
   BOT_INBOX_SEND_ENABLED=true
   # IDs below are EXAMPLES; order must match the server's BOT_TOKENS exactly.
   BOT_INBOX_BOT_IDS=8880029922,8803993138
   ```
   On every outgoing message, the backend calls Telegram `getMe` and
   refuses to send if the selected bot id does not match that token.
   It also refuses to send to a chat that the selected bot has never seen.

## Expected behavior and limits

- Incoming new **private, group, supergroup and channel** posts appear after
  `BOT_INBOX_ENABLED=true` and a polling restart.
- An existing group may only deliver messages to the bot permitted by
  Telegram group privacy settings and permissions; NoyaBot cannot silently
  scrape unseen conversations.
- The inbox lists only chats with observed messages. Historical messages
  which Telegram never delivered to the bot will not magically appear.
- Incoming media is stored as **type + file id**, not downloaded or served
  automatically. Rich media/file preview remains an upstream Web-client
  feature where it works. The DB inbox currently sends **plain text only**.
- Common text replies through `reply_noya_answer` and replies sent from the
  staff inbox are archived as outgoing messages. Other media-generating paths
  may need separate outbound instrumentation.
- The previous group-only `MessageSnapshot` is deliberately not auto-merged:
  it has **no bot identity**. Only if you have independently verified which
  bot originally observed a specific group, use the DRY RUN command:
  `manage.py import_legacy_group_inbox --bot-id BOT_ID --chat-id NEGATIVE_GROUP_ID`.
  Once confident, add `--apply`. Never import arbitrary history into both
  bots or infer private-message ownership.
- Retain only what you need: set `BOT_INBOX_RETENTION_DAYS` and schedule
  `manage.py purge_bot_inbox` daily. Full message archival is an explicit
  privacy-impacting deployment choice.
- The staff API returns conversation content, so protect the domain with
  authentication, TLS and strict access controls. The upstream JavaScript
  app is run under a **different origin** from the Django staff app, to
  isolate its browser token/session from Django administrative cookies.
- Outbound Telegram writes can have an unknown outcome if the request times
  out after Telegram accepts it. The UI never automatically retries a send.

## Smoke test

After deployment, send a private message to @NuyaRobot, then send a
normal group message visible to the bot. In the DB inbox, switch to the
numeric ID of @NuyaRobot, confirm both chats appear, open a chat and inspect
its contents. Switch to @iNuyaRobot: private messages from Noya must **not**
appear. If you enable sending, test using only a disposable private chat and
ensure a successful send is recorded in the archive.

Do not share live Telegram bot tokens, API keys, or the full .env in logs.

## Next iteration: one merged interface

For a truly unified single React sidebar, a separate upstream GPL fork
must add a DB-fed backend adapter and message cache merge into its own
`updateManager` and chat-list stores. That integration is not claimed here.
The stable database API now provides the required per-bot contract to build
this integration without inventing Telegram's unavailable `getHistory`.
