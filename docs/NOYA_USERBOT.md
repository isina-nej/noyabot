# Noya on a personal Telegram account (MTProto / Telethon)

This is a **separate process in the same Django repository**, not a replacement
for `runbot`. It runs Noya's **existing AI persona, tool catalog, memory
retrieval/ingestion, image generation, TTS, sampled Vision/STT and bounded
multi-message output** through an account owner's own Telegram session.

The account owner must explicitly authorize the session on a trusted terminal.
A `.session` file is a live Telegram login credential: do not upload,
forward, commit, log or send its contents to anyone.

## Safety and permissions

- The service sends **nothing** to unlisted dialogs. By default the
  `NOYA_USERBOT_ALLOWED_CHAT_IDS` allowlist is empty, so it only responds
  in the account's Saved Messages after an explicit `/noya ...` command.
- For an allowed group, it answers only when addressed as `نویا`, `Noya`,
  `@your_user_account`, `/noya`, or when somebody replies to the account's
  own message.
- For an allowed private DM, it answers incoming messages. Use this only when
  the correspondent is aware of the automated assistant.
- No bulk DMs, unsolicited replies, membership changes, forwarding chats,
  credential requests or cross-dialog actions. No model-selected chat targets.
- Ignore other bot senders and all outgoing messages (except explicit
  `/noya` in Saved Messages). Restrict simultaneous calls and per-chat rate.
- Authenticated account messages remain in that dialog. The persistent
  Noya conversation/memory is isolated from the BotFather bot's existing
  private-chat data with a separate numeric conversation namespace.
- Bot API-only administrative actions and BotFather-owned file references
  do **not** magically become MTProto abilities. Native media processing
  and delivery use the user account's own accessible messages.
- Using Telegram user accounts for automated replies may be subject to
  Telegram platform limits. Keep use opt-in, conversational and low-volume.

## Initial login

Telegram API credentials come from <https://my.telegram.org/apps>. Reuse
`TELETHON_API_ID` and `TELETHON_API_HASH` when appropriate, but use a
**different session file from `TELETHON_SESSION_PATH`**, which the
read-only channel scanner may already have open. The bot's
`BOT_TOKEN` remains untouched and is never used by this runtime.

Add these keys to the server's existing **private** `/opt/noyabot/.env`:

```dotenv
NOYA_USERBOT_API_ID=123456
NOYA_USERBOT_API_HASH=your-private-telegram-api-hash
NOYA_USERBOT_SESSION_PATH=/var/lib/noya-userbot/account
NOYA_USERBOT_ALLOWED_CHAT_IDS=
NOYA_USERBOT_SAVED_MESSAGES=true
```

Keep the original `NOYA_API_URL`, `NOYA_API_KEY`, model and image/TTS/STT
configuration; both transports use the existing generators.

Recommended dedicated Unix service account:

```bash
sudo useradd --system --home-dir /var/lib/noya-userbot --shell /usr/sbin/nologin noya-userbot
sudo install -d -m 700 -o noya-userbot -g noya-userbot /var/lib/noya-userbot
sudo chmod 600 /opt/noyabot/.env
cd /opt/noyabot
# Run only from a local/trusted interactive terminal. Django loads .env.
sudo -u noya-userbot .venv/bin/python manage.py login_noya_userbot
```

Telegram sends a one-time login code; type it into **your own terminal**.
If enabled, you are also prompted for the account's two-step password. The
login command doesn't print either secret and locks the resulting
`/var/lib/noya-userbot/account.session` to mode 600. Never send the code
or session file in a ChatGPT message.

Start with a **manual one-shot** so you can validate everything:

```bash
cd /opt/noyabot
sudo -u noya-userbot .venv/bin/python manage.py run_noya_userbot
```

In the authorized account's Saved Messages, send `/noya سلام` and check
the reply. To test image, send `/noya یه عکس از یک گربه بساز`; for voice,
`/noya این شعر رو ویس بخون: ...`.

## Enable specific chats

Only after Saved Messages works, add the numeric target Telegram dialog IDs:

```dotenv
NOYA_USERBOT_ALLOWED_CHAT_IDS=-1001234567890,123456789
```

The negative `-100...` ID is for a supergroup, the positive ID for a
one-to-one dialog. In a group, use `نویا سلام`, `/noya خلاصه کن`,
`@your_user_account بیا`, or reply to a message the account previously
sent. Nothing is posted to unrelated dialogs.

## Run as a system service

```bash
sudo cp deploy/noya-userbot.service /etc/systemd/system/noya-userbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now noya-userbot
sudo systemctl status noya-userbot --no-pager
sudo journalctl -u noya-userbot -n 60 --no-pager
```

Keep `tinkera-robot` running unchanged; the userbot service is independent.
Stop via `sudo systemctl stop noya-userbot`.

## Media and wait behavior

Photos, GIFs and static/video/TGS stickers are sampled into bounded visual
frames; video notes additionally use the existing STT provider. User-account
messages/replies are downloaded on demand, not bulk archived. Generated
images are sent as Telegram photos; generated TTS is encoded to OGG/Opus
and sent as a playable voice note. `send_chat_messages` and
`send_media_sequence` continue to work. Native reactions are attempted
where Telegram allows them.

An operation lasting over one minute may post a waiting status. It is
deleted **only after the real reply succeeds**. Inaccessible files or
provider failures are reported without guessing media contents.

## Diagnostics and limits

Run `python manage.py test botapp.test_noya_userbot`. Inspect only
sanitized service logs:

```bash
sudo journalctl -u noya-userbot --since "20 minutes ago" --no-pager |
grep -E 'NOYA-USERBOT|NOYA-AGENT|NOYA-VISION|ERROR'
```

The app does not include a Bot API-style full moderation/admin UI for the
user account; extending those actions requires **separate MTProto
authorization and tests**. Never run this worker against a session owned by
someone else without their explicit permission.
