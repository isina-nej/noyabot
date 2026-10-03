# HARNESS_ARCHITECTURE.md — Noya Harness Phase 1 (FROZEN)

> DESIGN_GATE: APPROVED
> CONTRACTS_FROZEN: YES
> Registry P0 baseline: `21ca0ac41900ed841547c826d5afe338edce55e3`
> Phase: 1 (in-memory persistence, single-process)
> Status: source of truth for Agents B–G. No redesign. No new decisions.

Phase 1 adds a durable, auditable multi-step ReAct investigation harness
(`harness.investigate`) on top of the existing P0 registry/orchestrator.
It does NOT change chat-loop behavior, admin tool handlers, confirmation
lifecycle, or audit schema. Production files stay untouched except via the
protocols at the bottom of this document.

---

## 1. Component architecture

```
Telegram update
  └─ botapp/agent_handlers.py (agent_router + ArchiveMiddleware)
       ├─ classify.py :: should_route_to_agent()   (deterministic parse → Jev fast-path → LLM, fail-open to chat)
       └─ orchestrator.py :: handle_admin_command()
            ├─ target_chat.py :: resolve_target_chat()      (current | explicit | forward)
            ├─ parser.py :: parse()                         (deterministic AgentDecision | None)
            ├─ ai.py :: NoyaAgentProvider                   (structured AgentDecision / HarnessStep / RouteClassification)
            ├─ registry.py :: registry                      (5 chat + 54 admin, single store, two catalogs)
            ├─ permissions.py                               (admin role × bot capability, both must pass)
            ├─ risk.py :: resolve_risk()                    (registry wins upward, AI can only raise)
            ├─ confirmations.py + callbacks.py              (glass-key confirm lifecycle, atomic claim)
            ├─ audit_log.py :: record_audit()               (append-only AgentAuditLog)
            └─ harness entry: agent_tools/harness.py :: investigate()
                 └─ agent/harness.py :: run_investigation()
                      ├─ step planner (AI HarnessStep JSON | deterministic fallback)
                      ├─ Plan v1 validator (V1–V12)
                      ├─ RunStore (Phase 1: in-memory)
                      ├─ tool executor (read-only allowlist, C1–C9 enforced)
                      ├─ sandbox.py :: run_sandboxed() (run_code, sandbox-only)
                      └─ finisher (Persian answer grounded in observations)
```

Existing single-step admin path (`handle_admin_command` → tool.handler →
confirmation → `execute_confirmed`) is unchanged. Harness is a read-only
fan-out/fan-in loop that terminates in a `finish` answer; it never executes
write tools, never creates confirmations, never calls Telegram mutating APIs.

Ownership: each box has exactly one owner (see §10). Shared files are
read-only for harness except via protocol (see §11).

---

## 2. Contracts C1–C9 (frozen)

C1 — Single registry, two catalogs. One `ToolRegistry` store. `category`
is `"chat"` or `"admin"`. Chat loop may only ever see
`registry.chat_definitions()`. Admin tools reachable only via
`registry.get/has/all/names/ai_catalog` (admin catalog) and executed only
through `orchestrator.py` or the harness tool executor. `dispatch` /
`dispatch_async` refuse admin-category tools outright.

C2 — Registration never implies authorization. Every admin tool carries
explicit `permission`, `risk_level`, `requires_confirmation`,
`target_kind`, `capability`, `input_schema` (strict Pydantic), `handler`
(async `(ctx, bot, validated)`), `ai_selectable`. Fail-closed defaults:
no permission, highest risk, confirmation required, not AI-selectable.
`register_tool` raises (`TypeError`/`ValueError`) on missing/invalid
metadata — never silently defaults.

C3 — AI output is data, never code. Model may only emit validated
`AgentDecision` (single-step path) or `HarnessStep` (harness planner step).
`extra="forbid"` on `AgentDecision`; unknown fields rejected. Untrusted
content (reply excerpts) wrapped in `<untrusted_message>` and treated as
data. Model cannot invent tools, run code outside `run_code`, read secrets,
or bypass allowlists.

C4 — Registry risk wins upward. `resolve_risk(registry_risk, ai_risk)`
returns the stricter of the two. AI can raise risk, never lower it.
`high` risk always requires confirmation on the single-step path.

C5 — Dual-gate authorization on every execution. Both
`ensure_admin_permission(identity, tool.permission)` (role matrix) and
`ensure_bot_capability(caps, tool.capability)` (Telegram capability) must
pass. Harness tool executor re-checks both per step. Confirmation
execution (`execute_confirmed`) re-resolves identity (no cache) and
re-checks both gates with immutable stored parameters.

C6 — Reply is the single source of truth for targets. For
`target_kind="member"`, `target_user_id` comes from `ctx.reply.user_id`;
for `target_kind="message"`, `message_id` comes from `ctx.reply.message_id`.
AI-provided ids are ignored when reply exists; missing reply + missing id →
`AmbiguousTarget` (fail-closed, reply required). Protected targets (bot
itself, group admins; fail-closed on lookup failure) raise
`ProtectedTarget`.

C7 — Harness is read-only by construction. Inside a harness run only tools
with `risk_level == "low"` AND `requires_confirmation == False` AND
`category == "admin"` AND present in the run's explicit allowlist may
execute. No confirmation creation, no `queue_or_execute`, no direct
Telegram mutating calls, no settings writes. `harness.investigate` itself
is `LOW`, `requires_confirmation=False`, `permission=analytics.read`,
never auto-runnable from inside a run (self-call refused).

C8 — Audit everything, leak nothing. Every admin request and every harness
run/step writes `AgentAuditLog` rows via `record_audit` with sanitized
parameters (secret-hint keys redacted, strings truncated). Raw confirm
tokens never enter DB/logs/audit (only SHA-256 `token_hash` stored).
Harness steps correlate via `request_id = "harness:<run_id>:<step_id>"`
(see §14). No tokens, keys, secrets, headers, full prompts, cross-group
data, or raw traces in user-visible output.

C9 — Fail-open to safe, fail-closed on action. Classifier failure →
normal Noya chat (never a fake admin error). AI planner failure with
observations → `_fallback_answer(observations)`; first-step planner
failure → deterministic read-tool pass. Any write-path ambiguity →
deny (`AmbiguousTarget` / `AgentParseError` / `AgentPermissionDenied`).
Stuck `executing` confirmations are never auto-retried
(`fail_stuck_executing` marks failed only).

---

## 3. Plan v1 schema (frozen)

A run executes a sequence of `HarnessStep`s. The planner emits exactly one
step per turn. Schema (`botapp/agent/schemas.py :: HarnessStep`,
`extra="ignore"`, max lengths enforced):

```python
class HarnessStep(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: Literal["call_tool", "run_code", "finish"]
    tool: str = ""                                  # call_tool only
    args: dict[str, Any] = Field(default_factory=dict)  # reserved; Phase 1 tools take {}
    code: str = Field(default="", max_length=4000)  # run_code only
    answer: str = Field(default="", max_length=4000)  # finish only
    thinking: str = Field(default="", max_length=400)  # one short sentence
```

Phase 1 parameter rule: read tools execute with `validate_params({})`.
`args` is accepted by schema but IGNORED in Phase 1 (tools run with `{}`).
Agents B–G must not pass through `args` to handlers.

`run_code` payload contract: variable `data` (dict with `items`, `latest`,
`count`) is pre-bound; snippet must assign `result`. `run_code` never
receives tool args, chat ids, secrets, or live objects — only the JSON-safe
observations payload (§7).

`finish` contract: `answer` is the final Persian user-facing text, grounded
only in recorded observations. Empty `answer` → `_fallback_answer`.

Deterministic fallback plan (AI planner off or first-call failure):
`_DETERMINISTIC_TOOLS` in fixed order
(`analytics.get_message_activity_today`, `analytics.get_top_senders_today`,
`analytics.get_today_summary`, `group.get_member_count`,
`group.get_moderation_status`, `group.get_schedules`,
`audit.get_recent_actions`; skipped if unregistered), then one fixed
sandbox digit-extraction snippet, then `_fallback_answer` + provenance
footer. Suffix: `error="deterministic"`.

---

## 4. Validation rules V1–V12 (frozen)

Applied in order to every planner-emitted step before execution. First
failure wins; the step is recorded as a `note` observation and the loop
continues (no exception to user).

V1 — `action` ∈ {`call_tool`, `run_code`, `finish`}. Else `note/unknown_action`.
V2 — Schema-valid `HarnessStep` (types, max lengths). Parse/repair: one
structural repair attempt via planner; still invalid → deterministic
fallback (step 1) or `note` + continue (later steps).
V3 — `thinking` ≤ 400 chars, optional. Overlong → truncated, not rejected.
V4 — `call_tool`: `tool` must be in the run's frozen allowlist (computed
once at run start from C7 predicate over registered admin tools). Unknown
or non-allowlisted (including `harness.investigate`, any write/medium/high
tool) → `note/denied`, no execution.
V5 — `call_tool`: dual-gate re-check (`ensure_admin_permission`,
`ensure_bot_capability`) at step time. Failure → `note` with Persian
reason, no execution.
V6 — `call_tool`: params forced to `{}`; `tool.validate_params({})` must
succeed. Failure → `note`, no execution. (No caller-supplied params in
Phase 1.)
V7 — `run_code`: non-empty, ≤ 4000 chars. Empty → `note/code`, skip.
V8 — `run_code`: sandbox statutes only — `run_sandboxed(code, data=payload)`
with `timeout_seconds=2.0`, `max_chars=4000`. No imports/network/fs/process
(AST + substring gates + tiny builtins + `math`/`statistics`/`json`).
Sandbox failure → `Observation(kind="code", name="run_code",
summary="SandboxError: …")`, loop continues.
V9 — `run_code` output must be JSON-serialisable (else stringified) and is
truncated to `MAX_OBSERVATION_CHARS`. Raw stdout captured, truncated same.
V10 — `finish`: `answer` ≤ 4000 chars, Persian, no claims beyond
observations. Empty → `_fallback_answer`. Overlong → truncated.
V11 — Budget: `steps_used ≤ max_steps` (`AGENT_HARNESS_MAX_STEPS`,
default 8; code default `MAX_STEPS_DEFAULT=5` when setting absent).
Exceeding → terminal `_fallback_answer`, `error="max_steps"`.
V12 — Observation hygiene: every stored observation summary truncated to
`MAX_OBSERVATION_CHARS` (3500); memory window to planner is last
`MAX_MEMORY_ITEMS` (12) formatted lines; fallback answer uses last 8
non-note observations truncated to 500 chars each.

---

## 5. HarnessRun / HarnessStep contracts (frozen)

```python
@dataclass
class Observation:
    kind: str   # "tool" | "code" | "note"
    name: str   # tool name | "run_code" | "denied" | "code" | "unknown_action"
    summary: str            # truncated user-safe text
    data: Any = None        # JSON-safe payload (tool text | {"result","stdout"} | None)

@dataclass
class HarnessRun:
    run_id: str             # uuid4 hex, created per run_investigation() call
    chat_id: int
    requester_user_id: int
    user_text: str          # truncated to AGENT_MAX_COMMAND_LENGTH (2000)
    max_steps: int
    allowlist: list[str]    # frozen at run start (C7 predicate)
    observations: list[Observation]
    status: str             # "running" | "finished" | "fallback" | "max_steps" | "disabled"
    steps_used: int
    error: str = ""         # "" | "deterministic" | "llm_step_failed" | "max_steps" | "harness_disabled"

@dataclass
class HarnessResult:
    ok: bool
    answer: str
    steps_used: int = 0
    observations: list[Observation] = field(default_factory=list)
    error: str = ""
```

Run identity: `run_id = uuid4().hex` per top-level `run_investigation()`
call. Step identity: 1-based index within the run. Audit correlation key:
`request_id = f"harness:{run_id}:{step_id}"` with `step_id` zero-padded to
3 digits (`001…`). Final `finish` row reuses the terminal step id. The
deterministic fallback path writes one audit row per executed tool with the
same key scheme (no separate run row in Phase 1).

`Observation.data` for `kind="tool"` is the truncated tool text (str).
For `kind="code"` it is `{"result": …, "stdout": …}` (JSON-safe).
For `kind="note"` it is `None` (summary only). Nothing else is stored.

---

## 6. State machine (frozen)

```
                    AGENT_HARNESS_ENABLED=false
   [entry] ────────────────────────────────────► [disabled] → HarnessResult(ok=False, error="harness_disabled")
     │
     │ enabled; user_text truncated; run_id minted; allowlist frozen
     ▼
 [running step=1..max_steps]
     │
     ├─ planner raises ──► observations? ──yes──► [finished llm_step_failed] → _fallback_answer
     │                     └─no──► deterministic pass ──► [fallback deterministic]
     │
     ├─ action=finish ──► V10 ──► [finished] → answer (or _fallback_answer)
     ├─ action=call_tool ──► V4,V5,V6 ──► pass: execute, append Observation(tool)
     │                                      fail: append Observation(note), continue
     ├─ action=run_code ──► V7,V8,V9 ──► append Observation(code), continue
     └─ unknown action ──► append Observation(note/unknown_action), continue
     │
     │ steps exhausted without finish
     ▼
 [max_steps] → _fallback_answer, error="max_steps"
```

Terminal answers: `ok=True` for `finished`, `fallback`, `max_steps`
(answer always present); `ok=False` only for `disabled`. `steps_used` is
the 1-based terminal step (or total observations for deterministic path).

Confirmation state machine (`confirmations.VALID_TRANSITIONS`) is OUT OF
SCOPE for harness: harness never creates, claims, or transitions
confirmations. Quoted here only as a boundary: pending → {executing,
confirmed, cancelled, expired}; confirmed → executing; executing →
{executed, failed}. Double-claim impossible (`select_for_update`).

---

## 7. Security boundaries (frozen)

1. Chat/admin catalog isolation. Chat model sees 5 chat definitions only.
   `dispatch/dispatch_async` return `"not available in this context"` for
   any admin name. Even a disobedient model cannot reach admin handlers via
   the chat loop.
2. Harness allowlist = explicit predicate, NOT prefix matching. Frozen rule:
   new Harness authorization code must NOT reference `_WRITEISH_PREFIXES`.
   The legacy `_is_auto_runnable`/`_WRITEISH_PREFIXES` helper in
   `agent/harness.py` is grandfathered for the current file only and must
   not be copied, imported, or extended. New executor predicate:
   `tool.category == "admin" and tool.risk_level == LOW and not
   tool.requires_confirmation and tool.name != "harness.investigate" and
   tool.name in run.allowlist`, plus per-step dual-gate (C5).
3. Target confinement. Harness tools run with `{}`; target-bearing tools
   (`target_kind != "none"`) fail `validate_params({})` (V6) and therefore
   cannot execute inside a run. `target_source` frozen to `reply | none`
   (schema `TargetSource` retains `user_id|self` only for the single-step
   AI bag; harness never uses them — reply-or-nothing).
4. Sandbox confinement (§3, V8). `run_code` operates solely on the `data`
   observations dict. Forbidden: imports, `__`, `open(`, `os.`, `sys.`,
   `subprocess`, `socket`, `pathlib`, `builtins`; AST bans
   Import/With/Raise/Try/ClassDef/FunctionDef/Lambda/Yield/Await/Global/
   Nonlocal + dangerous builtins + dunder attributes. 2 s timeout.
5. Secret hygiene. `AgentContext.ai_payload` is the only model-facing
   context (no tokens/secrets/histories). `sanitize_parameters` redacts
   secret-hint keys. Raw confirm tokens live only in `callback_data`
   (≤46 bytes, 64-byte Telegram limit respected).
6. Channel restriction. `_CHANNEL_UNSUPPORTED_TOOLS` (`member.mute/unmute/
   ban/unban/warn/get_warnings`, `group.lock/unlock`) rejected in channels
   with `AgentParseError`. Applies to harness entry routing equally.
7. Fail-closed write protection. Harness path contains zero write-capable
   call sites by construction (V4+V6). Any future tool with
   `risk != low` or `requires_confirmation` is automatically excluded.

---

## 8. Ownership table (frozen)

| Area | Owner agent | Files |
|---|---|---|
| Harness loop + Plan validation (V1–V12) | B | `botapp/agent/harness.py`, `botapp/agent_tools/harness.py` |
| RunStore abstraction + in-memory impl | C | NEW `botapp/agent/runstore.py` (+ tests) |
| Audit correlation (`harness:<run_id>:<step_id>`) | D | `botapp/agent/audit_log.py` (appenditive), `botapp/models.py` (`AgentAuditLog` — additive migration only) |
| Sandbox hardening / `run_code` semantics | E | `botapp/agent/sandbox.py` |
| Planner prompts + `HarnessStep` schema | F | `botapp/agent/ai.py`, `botapp/agent/schemas.py` (`HarnessStep` only) |
| Registry catalog semantics + tool metadata | G | `botapp/agent/registry.py`, `botapp/agent/tools.py`, `botapp/agent_tools/*.py` (metadata only, no handler logic changes) |
| Contract doc (this file) | A (done) | `HARNESS_ARCHITECTURE.md` only |

No agent edits outside its row without a `CROSS_AGENT_CHANGE_REQUEST`
(§15). `orchestrator.py`, `permissions.py`, `confirmations.py`,
`callbacks.py`, `context.py`, `risk.py`, `errors.py`, `parser.py`,
`target_chat.py`, `classify.py`, `responses.py` are SHARED — read-only for
all agents in Phase 1 (see §11).

---

## 9. Shared-file rules (frozen)

1. Read-only shared files (Phase 1): `orchestrator.py`, `permissions.py`,
   `confirmations.py`, `callbacks.py`, `context.py`, `risk.py`,
   `errors.py`, `parser.py`, `target_chat.py`, `classify.py`,
   `responses.py`, `config/settings.py`, `botapp/models.py` (except D's
   additive audit migration), `botapp/agent_handlers.py`.
2. A test may import from shared files but must not modify them.
3. New files allowed only: `botapp/agent/runstore.py`,
   `botapp/test_harness_*.py` (per-agent suites), this doc.
4. No changes to tool handlers, prompts outside F's scope, confirmation
   lifecycle, audit redaction, or registry dispatch semantics.
5. Every PR touches files in exactly one ownership row (+ tests). Cross-row
   needs an approved `CROSS_AGENT_CHANGE_REQUEST`.
6. Verification per change: `manage.py check`, target test module(s),
   `git status --short` shows only intended files.

---

## 10. Frozen safety limits (frozen)

| Knob | Setting | Code default | Notes |
|---|---|---|---|
| Harness master switch | `AGENT_HARNESS_ENABLED` | `True` | `false` → `HarnessResult(ok=False, error="harness_disabled")` |
| Harness AI planner | `AGENT_HARNESS_AI_ENABLED` (+ `AGENT_AI_ENABLED`) | `True` | off → deterministic read-tool pass, `error="deterministic"` |
| Max steps / run | `AGENT_HARNESS_MAX_STEPS` | `8` (`MAX_STEPS_DEFAULT=5` when setting absent) | planner loop cap; terminal `error="max_steps"` |
| Observation chars | — | `MAX_OBSERVATION_CHARS=3500` | per-observation truncation |
| Planner memory window | — | `MAX_MEMORY_ITEMS=12` | last-12 formatted lines |
| Fallback answer window | — | last 8 non-note obs × 500 chars | `_fallback_answer` |
| Sandbox code size | — | `max_chars=4000` | `HarnessStep.code` also ≤4000 |
| Sandbox wall clock | — | `timeout_seconds=2.0` | per `run_code` call |
| Finish answer size | — | `HarnessStep.answer` ≤4000 | enforced by schema |
| Planner thinking | — | ≤400 chars | `HarnessStep.thinking`, `RouteClassification` same |
| Command length | `AGENT_MAX_COMMAND_LENGTH` | `2000` | truncated at orchestrator entry |
| AI confidence floor | `AGENT_MIN_CONFIDENCE` | `0.80` | below floor + ≥0.5 + harness on → route `harness.investigate`; below 0.5 → `AgentParseError` |
| Route confidence | — | `0.72` (`_MIN_ROUTE_CONFIDENCE`) | Jev fast-path: agent ≥0.80, chat ≥0.70, else LLM fallback |
| Confirm TTL | `AGENT_CONFIRMATION_TTL_SECONDS` | `120` | single-step path only; harness creates none |
| Chat loop iterations | — | `MAX_ITERATIONS=8` | Noya chat loop, unrelated to harness steps |
| Chat budgets | — | `MAX_SEARCHES=3`, `MAX_FETCHES=6` | chat tools only |

No agent may raise a limit without an `ARCHITECTURE_CHANGE_REQUEST` (§16).

---

## 11. Registry catalog semantics (frozen, P0 baseline `21ca0ac`)

Chat catalog — exactly 5 (`category="chat"`, legacy dict `parameters`,
sync/async handlers in `botapp/agent/tools.py`, executed via
`dispatch/dispatch_async` only):
`web_search`, `fetch_url`, `get_time`, `generate_image`, `text_to_speech`.

Admin catalog — exactly 54 (`category="admin"`, strict Pydantic
`input_schema`, async `(ctx, bot, validated)` handlers, executed only via
orchestrator or harness tool executor):

- analytics (7): `analytics.get_today_summary`,
  `analytics.get_period_summary`, `analytics.get_top_moderated_users`,
  `analytics.get_message_activity_today`,
  `analytics.get_message_activity_period`,
  `analytics.get_top_senders_today`, `analytics.generate_briefing`
- audit (2): `audit.get_recent_actions`, `audit.get_actions_for_member`
- channel (7): `channel.get_info`, `channel.get_subscriber_count`,
  `channel.get_admins`, `channel.delete_post`, `channel.pin_post`,
  `channel.unpin_post`, `channel.post_text`
- group (11): `group.get_info`, `group.get_member_count`,
  `group.get_admins`, `group.get_bot_permissions`, `group.get_settings`,
  `group.get_moderation_status`, `group.get_schedules`,
  `group.add_daily_schedule`, `group.remove_daily_schedule`,
  `group.lock`, `group.unlock`
- harness (1): `harness.investigate`
- member (7): `member.get_info`, `member.get_warnings`, `member.warn`,
  `member.mute`, `member.unmute`, `member.ban`, `member.unban`
- message (5): `message.delete`, `message.pin`, `message.unpin`,
  `message.get_bot_deleted_recent`, `message.get_recent_archived`
- settings (14): `settings.enable_anti_spam`, `settings.disable_anti_spam`,
  `settings.enable_anti_link`, `settings.disable_anti_link`,
  `settings.enable_anti_forward`, `settings.disable_anti_forward`,
  `settings.set_flood_limit`, `settings.set_max_warnings`,
  `settings.get_blocked_words`, `settings.add_blocked_word`,
  `settings.remove_blocked_word`, `settings.get_allowed_domains`,
  `settings.add_allowed_domain`, `settings.remove_allowed_domain`

Invariants (see `botapp/test_registry.py`): chat definitions expose exactly
the 5; `member.ban` absent from chat; `registry.names()` holds admin names
(≥50); admin dispatch on chat path returns `"not available in this
context"`; every admin tool has real async handler + Pydantic schema +
known permission/capability/target; all `high`-risk tools require
confirmation. Count check: 5 + 54 = 59 registered names (plus 2 legacy
empty-`name=""` artifacts in grep output that must NOT become tools).

Harness-readable subset (C7 predicate at baseline): all `LOW` +
`requires_confirmation=False` admin tools — the 7 analytics, 2 audit,
`group.get_info/member_count/admins/bot_permissions/settings/
moderation_status/schedules`, `member.get_info/get_warnings`,
`message.get_bot_deleted_recent/get_recent_archived`,
`settings.get_blocked_words/get_allowed_domains`, `channel.get_info/
subscriber_count/admins` — minus nothing else; `harness.investigate`
explicitly excluded from its own allowlist. Concrete allowlist is computed
at run start from live registry (so future LOW tools join automatically
only if they satisfy the full predicate).

---

## 12. RunStore abstraction (frozen interface)

```python
class RunStore(Protocol):
    def create(self, run: HarnessRun) -> HarnessRun: ...
    def append_observation(self, run_id: str, obs: Observation) -> None: ...
    def set_terminal(self, run_id: str, *, status: str, answer: str = "", error: str = "") -> None: ...
    def get(self, run_id: str) -> HarnessRun | None: ...
    def recent(self, *, chat_id: int | None = None, limit: int = 20) -> list[HarnessRun]: ...
```

Rules: store is write-through per step (create → append × N →
set_terminal). `get` returns a defensive copy. `recent` newest-first,
 capped at `limit ≤ 50`. No querying by user text content (only ids,
 chat, status). No persistence of secrets (observations already truncated/
 sanitized upstream). Interface lives in NEW `botapp/agent/runstore.py`
 (owner: C). Orchestrator/harness depend on the `Protocol`, never the
 implementation.

---

## 13. In-memory Phase 1 persistence (frozen)

`InMemoryRunStore`: `dict[str, HarnessRun]` guarded by `threading.Lock`,
bounded (default `max_runs=200`, evict oldest-first). Process-local:
no DB, no cross-process visibility, no TTL beyond process life. Eviction
drops full runs only (never partial observations of a live run). `clear()`
exists for tests only. Failure mode: store errors never fail the user
answer — log + continue with local `observations` list as fallback.
Phase 2 (out of scope) may add a Django-backed store behind the same
`Protocol` without changing callers.

---

## 14. Audit correlation (frozen)

Every harness step writes an `AgentAuditLog` row with:

- `request_id = f"harness:{run_id}:{step_id:03d}"` — e.g.
  `harness:9f2c…:003`. Literal `harness:` prefix, `run_id` hex, 3-digit
  step. Deterministic-fallback tool rows use the same scheme.
- `tool_name` = executed tool / `"run_code"` / `"harness.finish"` /
  `"harness.denied"` for notes.
- `parameters` = `{}` for tools, `{"code_len": N}` for `run_code`,
  `{}` for finish/notes (never raw code/answer beyond existing 200-char
  sanitize cap).
- `parser_type = "harness"`, `detected_intent = <run user_text[:64]>`,
  `execution_status` ∈ {`started`, `executed`, `failed`},
  `confirmation_status = "not_required"`, `risk_level = "low"`,
  `error_code` ∈ {`"", "deterministic", "llm_step_failed", "max_steps",
  "sandbox_error", "denied", "harness_disabled"}`.
- One extra terminal row (`step_id` = terminal step) with the final
  `execution_status` and `error` value, so a run is countable as
  `N step rows + 1 terminal row` by `request_id` prefix
  `harness:<run_id>:`.

`record_audit` itself is unchanged (owner D may only add a thin
`record_harness_step()` wrapper that fills these fields and delegates).

---

## 15. Fixed semantics (frozen, no exceptions)

- `target_source = reply | none`. Inside harness, target-bearing tools
  cannot run (V6). The single-step `ToolParameters.target_source` bag
  keeps its 4-value type for parser compatibility, but harness path
  honors only `reply` (resolve from `ctx.reply`) and `none` (no target).
  `user_id`/`self` values are treated as `none` + `AmbiguousTarget` note
  if a target would be required.
- `run_code` is internal sandbox-only. It computes over `data`
  (observations) and returns `result`. It cannot call tools, import,
  access network/fs/process/env, read messages, or persist. Its output
  is an observation like any other — never executed, never trusted as
  instruction.
- No `_WRITEISH_PREFIXES` in new Harness authorization. The tuple in
  `agent/harness.py` is legacy-grandfathered for that file only. Agent B's
  executor and any future authorization code MUST use the §7.2 predicate
  (explicit metadata comparison). Grep gate: `rg _WRITEISH_PREFIXES
  botapp/agent/` must return exactly the one definition + its local uses
  after Phase 1.
- Multi-step entry policy. Exactly one multi-step entry:
  `harness.investigate` (LOW, no confirm, `permission=analytics.read`).
  Entry routes: deterministic `parse()` phrases («بررسی کامل / تحلیل عمیق
  / …»), or AI `AgentDecision(tool="harness.investigate")` when confidence
  ∈ [0.5, `AGENT_MIN_CONFIDENCE`), or explicit AI selection for
  multi-source questions. Guards at entry: caller must pass full
  orchestrator gates (admin, capability, channel-support); `ctx` is
  captured once and reused for all steps (no re-resolution of target
  chat mid-run); nested `harness.investigate` calls refused (V4).
  Single-shot tools (`analytics.generate_briefing` for «تحلیل کن») stay
  single-step and never auto-escalate to harness.

---

## 16. CROSS_AGENT_CHANGE_REQUEST protocol (frozen)

Needed when an agent must touch outside its ownership row (§8) or a shared
file (§9).

```
CROSS_AGENT_CHANGE_REQUEST
from: <agent>        to: <owning agent>
file(s): <paths>     reason: <why local fix impossible>
diff-sketch: <≤10 lines>   rollback: <how to revert>
```

Rules: file posted as a code block in the agent channel; owner replies
`APPROVED <sha?>` / `REJECTED <reason>` within the same task turn. On
approve, requester makes the minimal edit, adds a regression test in their
own suite, and cites the approval SHA in the commit body
(`Cross-agent: APPROVED by <owner>`). No approval → no edit; requester
works around within their row and notes the limitation in their report.
Emergency production breakage: owner may hot-fix directly and notify.

---

## 17. ARCHITECTURE_CHANGE_REQUEST protocol (frozen)

Needed for any change to this document's frozen items: contracts C1–C9,
Plan schema, V1–V12, Run/Step dataclasses, state machine, boundaries,
ownership, shared-file list, safety limits, catalog counts, RunStore
interface, correlation format, fixed semantics.

```
ARCHITECTURE_CHANGE_REQUEST
proposer: <agent>    date: <YYYY-MM-DD>
section: <§ + item>  change: <old → new>
justification: <test/log evidence>   blast-radius: <agents/files>
```

Rules: requester opens the request, STOPS related work, and waits.
Architecture Guardian (Agent A) decides: `APPROVED` (doc amended, version
bumped, all agents notified) or `REJECTED` (work continues under frozen
text). No agent self-approves. Two approvals never stack — one Guardian
decision is final for Phase 1.

---

## 18. Agent report contract (frozen)

After work, each agent reports exactly:

```
CONTRACT_DOC_READY: YES/NO
COMMIT_SHA:
FILES_CHANGED:
CONTRACTS_FROZEN: YES
```

Agent A (this doc) additionally verifies `git status --short` shows only
`HARNESS_ARCHITECTURE.md` before committing as
`docs(harness): freeze phase 1 architecture contracts`.
