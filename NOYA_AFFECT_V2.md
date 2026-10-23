# Noya emotional persona v2 — architecture, rollout and limitations

This feature is **not a claim of actual human sentience**. Its 31 scores are
bounded controls for narrative conversational style; they are not clinical
measurements and do **not** add up to 100%.

## Existing architecture and observed failure modes

- `botapp/emotions.py` maintains per-conversation hurt/repair ledgers.
- `botapp/memory/relationship_events.py` recognizes **one** event, and a
  positive emoji/affection can veto another cue (mixed message bug).
- `botapp/memory/integration.py` read the legacy ledger only at Noya response time.
- `botapp/agent_handlers.py:ArchiveMiddleware` sees each delivered update
  but previously did not persist a user-global affect vector.
- `botapp/ai/prompts.py` has a long stable persona prompt; character lore
  previously had no database-driven, query-conditional retrieval.

## New behavior

`botapp/affect.py`: 31 independent dimensions:
love, interest, affection, trust, respect, admiration, closeness, longing,
attachment, jealousy, anger, hatred, hurt, resentment, distrust, fear,
disgust, contempt, indifference, competition, pity, empathy, guilt, shame,
pride, disappointment, gratitude, curiosity, suspicion, security, attraction.

Default scores are mild and separate from emotion events. High-confidence
recognized cues can update **several dimensions in one message**. Each update
is attenuated by the current score (diminishing returns), and unmentioned
dimensions slowly return to their baseline after elapsed time **only if an
affective event is actually processed**. A neutral message is an absolute no-op.
The final prompt gets a small list of salient dimensions and behavioural
guidance, not the entire 31-number vector.

`botapp/memory/affect.py`: persists a single `NoyaAffectProfile` per
authenticated Telegram user ID, with a bounded list of processed Telegram
message IDs for best-effort idempotency. Updates require a message addressed
to Noya (bot DM, name/username in observed group, or verified reply to bot).
Every group/private update received by the bot can pass the shared middleware;
Noya does not have to generate a reply for affect observation.

The Telegram Bot API **cannot read unrelated private chats between third
parties** just because a bot's name is typed there. Only updates actually
delivered to the bot are eligible. This implementation does not attempt to
surveil private chats, resolve unverified aliases, or assume that forwarded
third-party text came directly from its purported author.

`NoyaPersonaFact`: trusted, editable superuser-controlled fictional biography
and episodes. The seed data includes a **fictional** age (25), degree area
(communication design), interests, a friendship misunderstanding, an old
friend moving away, and a gentle dramatic backstory; it is not a real human
biography, medical condition or proof of therapy qualification. A message
question must match curated triggers before at most three short lore records
are retrieved. The base prompt only defines core identity/tone, safe
boundaries, and truthfulness.

`/forget_all confirm` in the bot's own DM deletes the new user-global
emotion vector as well as the legacy relationship memory. The user can
regain neutral defaults. Adult gating for optional non-explicit attraction
is off by default; it must not be inferred from user text. Insults, hatred,
sexual pressure, possessiveness and manipulative tactics are not enacted.

## Code locations

- `botapp/affect.py` — pure multi-label engine, evolving percentages.
- `botapp/memory/affect.py` — persistence, addressing, persona fact retrieval.
- `botapp/agent_handlers.py` — passive group/DM delivery observation.
- `botapp/memory/integration.py` — active retrieval and composition of context.
- `botapp/ai/prompts.py` — stable policy for narrative tone/identity.
- `botapp/migrations/0034_noya_affect_profile_persona.py` — schema and seed.
- `botapp/test_affect_v2.py` — regression coverage.

## Deployment gate

This branch changes Django models and needs migration **0034**. Before
Production rollout, inspect the tests and apply migration only after a
verified database backup and a successful rehearsal against a staging copy.

**Do not use `git reset --hard`, `git clean -fd` or plain `git pull` against
`/opt/noyabot`**: the running server has substantial local changes and
untracked migrations. Prepare a clean worktree, review differential changes,
run `python manage.py check`, `makemigrations --check --dry-run`,
`python manage.py test`, and stage before any merge/deploy.
Never run a second Telegram polling process with the same bot token.

## Known gaps

- The current lexical cue extractor is conservative, not a calibrated
  Persian multi-label LLM emotion classifier; it will miss sarcasm,
  discourse-level implication and expressions not in its lexicon. It must
  eventually be supplemented with a schema-validated, opt-in classifier,
  evaluation set and confidence/target-resolution checks.
- The existing legacy hurt/repair ledger stays authoritative for its own
  conflict state; both layers coexist and do not yet fully synchronize.
- Independent percentages are heuristics and must be calibrated using
  consensual, privacy-safe evaluation data rather than interpreted as
  psychological truth.
- The initial processed-key window is bounded to 128; production-scale,
  indefinitely replay-safe deduplication requires a separate unique
  event ledger and retention policy.
- Persona lore is fictional. Do not present it as a real person's traumatic
  memories. Disclosure of artificial identity must remain truthful when
  directly asked.
