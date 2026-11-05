# Noya: scoped 200-message context, historical voice/video and identity

## What works

* When Noya is addressed by name, @mention or reply, the existing Telegram
  handler asks noya_history.archived_context_for_message for the last 200
  previously received and archived messages in the SAME group/topic and before
  the current message ID.
* The 200 messages are a scan budget, NOT 200 full messages sent to the model.
  Recent and keyword-relevant turns are selected (up to 30 lines / 8,000
  characters by default), reducing the impact on existing AI latency.
* Existing reply-chain, pictures, voice transcription and video-frame intake
  remain available. If background enrichment is explicitly enabled, newly
  observed audio/video is processed asynchronously. Only bounded text
  transcripts and optionally a description of sampled video frames are stored
  in MessageSnapshot.metadata (noya_media).
* Telegram-visible first_name, last_name and username are stored with a
  chat-scoped numeric Telegram user ID. These are NOT verified legal names.
  A user can set their own nickname with "منو X صدا کن" or /nickname X.
  **Replying to another human member with /nickname X assigns that member a
  group label**, distinctly stored from their own self-chosen nickname. The
  label is a claim by the assigning member, not verified identity. It is
  bound to the replied-to Telegram numeric user ID in this group. /nickname_clear
  without a reply clears your own nicknames; when replying it removes only a
  group label assigned by you. /forget_all confirm in a private chat deletes
  observed identity rows and group labels assigned by the requesting user.

## Platform limits

Telegram Bot API cannot retrieve arbitrary historical messages the bot
never received. Group bots need sufficient visibility (group admin or
Privacy Mode disabled in BotFather, with re-add to group afterward).
A pre-existing archive that lacks audio transcripts/video descriptions
cannot retrospectively supply that media content.

Archiving is opt-in. Inform members about storing and provider processing
of group voice and video. Respect the group policy, retention rules and
requests to remove personal data.

## Setup

Apply migrations BEFORE running the new code:

    python manage.py check
    python manage.py migrate
    python manage.py makemigrations --check --dry-run

Enable the archive (with the group's informed consent):

    MESSAGE_ARCHIVE_ENABLED=true
    MESSAGE_ARCHIVE_RETENTION_DAYS=30
    NOYA_HISTORY_ENABLED=true
    NOYA_HISTORY_SCAN_MESSAGES=200
    NOYA_HISTORY_SELECTED_LINES=30
    NOYA_HISTORY_MAX_CHARS=8000

For future media, only after checking provider/cost and group consent:

    NOYA_ARCHIVE_MEDIA_ENRICHMENT_ENABLED=true
    NOYA_ARCHIVE_VIDEO_VISION_ENABLED=true
    NOYA_VIDEO_SAMPLE_FRAMES=true

Configure a working STT provider and a vision-capable model, plus ffmpeg.
Respect Telegram Bot API download limits and your memory/CPU budget.
No raw audio, video, pictures, Bot tokens or API keys are stored in the
archive's media text metadata.

Processing is best-effort: one concurrent enrichment and no more than five
outstanding tasks per process. Restarts or heavy load may skip jobs. To
guarantee all media is processed, a durable queue, retry policy and per-group
consent controls must be added; this patch does not claim that guarantee.

Run the existing purge_message_snapshots maintenance job routinely:
specifying 30 days does not purge without scheduled execution.

## Tests

    python manage.py test botapp.test_noya_200_history botapp.test_noya_context botapp.test_media_intake
    python manage.py makemigrations --check --dry-run

Do not deploy an unreviewed branch directly to the public server. Benchmark
real group requests against the existing provider latency.
