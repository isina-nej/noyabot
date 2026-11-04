#!/usr/bin/env bash
# Obtain the genuine GPL Telegram Web for Bots source at the audited revision.
# The bot token is NEVER supplied to this build script.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REV="$(tr -d '[:space:]' < "$ROOT/upstream-revision.txt")"
DEST="${1:-/opt/telegram-web-for-bot}"
if [ -e "$DEST" ]; then
  echo "Target exists. Refusing to overwrite: $DEST" >&2
  exit 1
fi
git clone --filter=blob:none --depth=1 https://github.com/ohld/telegram-web-for-bot.git "$DEST"
(
  cd "$DEST"
  git fetch --depth=1 origin "$REV"
  git checkout --detach "$REV"
  echo "Pinned Telegram Web for Bots revision: $(git rev-parse HEAD)"
)
