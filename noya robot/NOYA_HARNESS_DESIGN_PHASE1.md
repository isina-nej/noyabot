# Noya Harness Core — Phase 1 Design (APPROVED)

DESIGN_GATE: APPROVED (2026-10-03)
CONTRACTS_FROZEN: YES — no changes without ARCHITECTURE_CHANGE_REQUEST + A/G decision.
Status: APPROVED. No production code written. No migrations. No registry/service/router edits.
Reconciled: 2026-10-03 against working tree HEAD `d9dc12b` + uncommitted Registry P0 (dirty: `botapp/agent/registry.py` +217/-36, `botapp/agent/tools.py` +5, `botapp/agent/loop.py` +4; untracked `botapp/test_registry.py`).
Verification: `.venv/bin/python` + Django setup. Counts: TOTAL 59 / ADMIN 54 / CHAT 5. `ai_catalog` len 54 (all admin `ai_selectable=True`). Chat `ai_selectable=False` × 5. Target kinds admin: none 41 / member 8 / message 5. Risk/confirm admin: low+no-confirm 26 / medium+confirm 19 / high+confirm 6 / medium+no-confirm 3 (`settings.enable_*` trio).
Source of truth: implementation, not README. Phase 0 file:line refs retained; P0-corrected values below supersede draft where noted.

Existing Harness (must not duplicate, reuse as-is):
- `botapp/agent/harness.py` — ReAct investigate: `MAX_STEPS_DEFAULT=5`, `MAX_OBSERVATION_CHARS=3500`, `MAX_MEMORY_ITEMS=12`, `_DETERMINISTIC_TOOLS` 7, `_WRITEISH_PREFIXES` blocklist, `_is_auto_runnable` (excludes `harness.investigate`, LOW-only, no-confirm, non-writeish leaf), `run_investigation` gated by `AGENT_HARNESS_ENABLED` / `AGENT_HARNESS_MAX_STEPS=8` / `AGENT_HARNESS_AI_ENABLED`.
- `botapp/agent_tools/harness.py` — `harness.investigate` thin adapter (ANALYTICS_READ, LOW, no-confirm, EmptyParams).
- New layer: **Harness Core** (`botapp/harness/`). Never edit `botapp/agent/harness.py` semantics. Reuse `run_investigation` as one step type behind planner/executor. `run_code` is internal sandbox-only, not an AI-selectable external tool (no network, no filesystem, no privileged execution — §4 pseudo-step only).

---

## 0. Reconciliation verdict (answers §1–§4 of review request)

1. Contracts vs Registry final: COMPATIBLE with 3 corrections applied in this file: (a) validator must branch chat/admin via `all_tools()` first — `registry.get/has` raise/reject chat names by contract (`get("web_search")` → `UnknownTool`); (b) chat-tool `risk_level/requires_confirmation` defaults (high/True) are meaningless — harness ignores them for `category==chat`, treats chat steps as dispatch-only non-privileged; (c) `wait_confirm/notify` removed from Plan tool enum — they are executor states, not tools. `run_code` kept as sole pseudo-step (sandbox-backed).
2. Catalog effect: chat exactly 5 (`web_search, fetch_url, get_time, generate_image, text_to_speech`), legacy `parameters` dicts, reachable only via `chat_definitions()/dispatch/dispatch_async`; `dispatch` refuses admin with `"not available in this context"` (test-asserted). Admin 54 reachable only via orchestrator path; `get/has/all/names/ai_catalog` admin-only. Harness chat steps → `dispatch_async` only. Harness admin steps → orchestrator confirm path only. No cross-execution.
3. Metadata fields harness consumes (§3 C2–C5, §4 V5–V7): `name, category, permission, capability, risk_level, requires_confirmation, target_kind, input_schema (+schema_dict/validate_params), ai_selectable, human_verb (nullable — fallback to name), description/parameters`. Detail in §3.
4. Ownership: table updated (§2) — added `botapp/test_registry.py` → G; noted dirty baseline must be committed by G before B–F. No other ownership change. B–F BLOCKED until approver gate.

## 1. Component diagram

```
User/Event (Telegram message, callback, channel post)
  │
  ▼
Intent Router (EXISTING: agent_handlers.py AgentTriggerFilter + classify.py + orchestrator deterministic_parse)
  │  chat ──────────────► Noya Chat (UNCHANGED: services.call_noya_api → loop.run_agent_loop, chat_definitions only)
  │  admin single-step ─► Admin Agent (UNCHANGED: orchestrator.handle_admin_command)
  │  multi-step ────────► Harness entry gate (NEW, thin, AFTER single-step parse, fail-open to chat)
                          │
                          ▼
                  Harness Planner (NEW, B) — proposes Plan, no auth, no execution, output untrusted
                          │
                          ▼
                  Plan Validator (NEW, B, deterministic, no LLM, no I/O)
                          │
                          ▼
                  Security Gate (NEW, C — calls EXISTING functions in EXISTING order, §7)
                    verdict per step: ALLOW / NEED_CONFIRMATION / DENY(code)
                          │
                          ▼
                  Workflow Executor (NEW, E, sequential Phase 1)
                    read/chat steps → dispatch_async path
                    admin/write steps → ONLY create_confirmation → (callback) → execute_confirmed
                    NEVER tool.handler() directly for admin tools; run_code ONLY run_sandboxed
                          │
                          ▼
                  Tool Layer (EXISTING, D catalogs — no duplication, no rewrite)
                          │
                          ▼
                  Observer (NEW, E — read-only: CONTINUE / WAIT_CONFIRM / REPLAN / FINISH / FAIL)
                          │
                    ┌─────┴──────┐
                    ▼            ▼
              Controlled      Noya Response (EXISTING: responses.py + Telegram send)
              Re-plan ──► Planner with ObservationEnvelope (DATA-only) ──► re-validate + re-authorize ──► bounded MAX_REPLANS
```

Admin callbacks (`agent_confirm:/agent_cancel:`) resume `WAITING_CONFIRMATION` via EXISTING `callbacks.process_confirm → claim_for_execution → execute_confirmed`. Harness invents no confirm channel. One active confirmation per run.

## 2. Agent ownership table (FINAL)

| Path | Owner | Notes |
|------|-------|-------|
| `botapp/harness/__init__.py` | B | package exports only |
| `botapp/harness/schemas.py` | B | Plan/Step wire schemas, frozen post-gate |
| `botapp/harness/plans.py` | B | Plan model, DAG, topo order |
| `botapp/harness/validation.py` | B | V1–V12 deterministic rules + codes |
| `botapp/harness/planner.py` | B | planner iface + deterministic fallback + AI adapter (allowlist = ai_catalog + chat 5) |
| `botapp/harness/security.py` | C | verdicts + `classify_access` + trust boundary (wraps existing fns, no reimplementation) |
| `botapp/harness/capabilities.py` | D | catalog matrix extension (data only, no registry edits) |
| `botapp/harness/executor.py` | E | step runner, budgets, cancellation, timeout, result passing |
| `botapp/harness/observer.py` | E | observation envelope + re-plan decision (no auth, no tools, no plan edits) |
| `botapp/harness/state.py` | E | Run/Step runtime objects, in-memory Phase 1 |
| `botapp/harness/limits.py` | E (values frozen §10) | constants only |
| `botapp/harness/errors.py` | B (codes), all use | `PlanRejected` codes V1–V12 + reuse of `errors.py` codes at runtime |
| `botapp/tests/test_harness_*.py` | F | simulation only (mocks/fakes; never live bot token), no prod edits |
| `HARNESS_ARCHITECTURE.md` | A | contract doc (this file is the content source) |
| `botapp/agent/registry.py` | G only | shared — P0 dirty baseline, commit first |
| `botapp/agent/tools.py` | G only | shared — P0 dirty (5× category="chat") |
| `botapp/agent/loop.py` | G only | shared — P0 dirty (chat_definitions 1-liner) |
| `botapp/test_registry.py` | G only | P0 contract tests (untracked) — G owns, F must not fork |
| `botapp/agent/orchestrator.py` | G only | admin path, no B–F edits |
| `botapp/agent/harness.py` | G only | existing investigate loop, reuse as-is |
| `botapp/agent_tools/*` | G only | admin tool defs; D deltas via CACR only |
| `botapp/models.py`, `migrations/*` | G only | NO migration Phase 1 |
| `botapp/services.py`, `views.py`, `api.py`, `runbot.py`, `agent_handlers.py`, `config/settings.py` | G only | wiring; Integrator connects |

Rule: two agents never edit same prod file. Cross-owner need → `CROSS_AGENT_CHANGE_REQUEST` (file, change, reason, interface, requester/phase). Contract change post-freeze → `ARCHITECTURE_CHANGE_REQUEST` (existing, proposed, reason, affected agents, migration impact). A/G decide. B–F start BLOCKED until gate approval.

## 3. Contracts between components (FROZEN)

Wire schemas Pydantic, `extra="forbid"` (precedent: `schemas.AgentDecision`). Dicts never cross boundaries unvalidated.

| # | Edge | Contract |
|---|------|----------|
| C1 | Router → Harness entry | `HarnessRequest{chat_id, requester_id, goal_text (1..2000, AGENT_MAX_COMMAND_LENGTH), reply: ReplyTarget\|None, chat_type, entry_confidence}`. Entry only on multi-step signal AND (admin identity resolvable OR pure-read research/analytics). Single-step admin stays in orchestrator. Fail-open to chat preserved. |
| C2 | Planner → Validator | `Plan{version:"harness.plan/v1", goal, steps, budget}`. Untrusted. Validator pure (no I/O, no LLM). Planner allowlist: admin names from `registry.ai_catalog()` (54) + chat names from `chat_definitions()` (5) + `"run_code"`. Nothing else plannable. |
| C3 | Validator → Security | `ValidatedPlan` + per-step `{tool_exists, params_valid, deps_acyclic, target_shape_ok, catalog}`. Invalid → `PlanRejected{code:V1..V12, step_id?, reason}` — never reaches Security. |
| C4 | Security → Executor | per-step `AuthVerdict{ALLOW \| NEED_CONFIRMATION \| DENY(code)}`. DENY codes reuse `errors.py`: `permission_denied, bot_capability_missing, protected_target, ambiguous_target, unknown_tool, invalid_tool_input, parse_error` + harness `channel_unsupported, high_risk_no_confirm, untrusted_trigger`. Executor must not execute NEED_CONFIRMATION/DENY. |
| C5 | Executor → Tool Layer | chat steps (`category==chat`): `registry.dispatch_async(name, args)` — str result, never raises. Admin steps (`category==admin`): `tool.validate_params()` already done in V6, then `create_confirmation` + (on callback) `execute_confirmed`. Direct `handler()` call for admin tools FORBIDDEN (static rule for F tests). `run_code`: `sandbox.run_sandboxed(code, data, timeout_seconds=2.0, max_chars=4000)` only. `harness.investigate` as a step calls existing `run_investigation` (recursion guard kept — never nested inside its own expansion, V10). |
| C6 | Tool Layer → Observer | `StepResult{step_id, status: OK/FAILED/DENIED/TIMEOUT/SKIPPED, output_ref (≤MAX_RESULT_SIZE, truncated), evidence: [{source_url, excerpt ≤500}] or None, duration_ms}`. External content tagged `untrusted`, never promoted to plan/targets. |
| C7 | Observer → Planner | `ObservationEnvelope{completed_steps, failed_step?, failure_code?, hints (DATA-only), replan_count}`. Hints cannot grant permission, name new privileged tools, set targets, or skip confirmation. Re-plan re-enters C2. |
| C8 | Executor → Audit | every attempt/success/failure/deny/confirm via EXISTING `audit_log.record_audit` (sanitized params, `_SENSITIVE_HINTS`). Run/step correlation: `request_id` formatted as `harness:<run_id>:<step_id>` — no new table, no new columns, no migration Phase 1. G decodes prefix in review/queries. Fixes existing `_run_tool` audit gap through G, not a parallel writer. |
| C9 | Harness → Response | `HarnessOutcome{status: COMPLETED/FAILED/CANCELLED/PARTIAL, summary_text, visible_steps, confirmation_prompt?}`. Personality (`ai/prompts.py`) untouched; text flows through existing response path. PARTIAL must disclose exactly what ran. |

Consumed Tool metadata (exact mapping to Registry final):
- `name` — plan reference; resolved via `all_tools()` dict first, then branched (never `get()` for chat names).
- `category` — `chat` → dispatch path, ignore permission/risk/confirm; `admin` → full gate. `VALID_CATEGORIES={chat,admin}` enforced at registration.
- `permission` (required for admin; e.g. `members.ban`, `analytics.read`) → `ensure_admin_permission`. `None` only valid for chat.
- `capability` (`can_restrict_members/can_delete_messages/can_pin_messages/can_post_messages`/None) → `ensure_bot_capability`. Chat: None/ignored.
- `risk_level` (low/medium/high, `RISK_ORDER`) → `resolve_risk(registry_risk, ai_risk)` (registry wins, only raise). Chat: ignored.
- `requires_confirmation` → `requires_confirmation = tool.requires_confirmation or risk==HIGH` (orchestrator:246). Chat: ignored.
- `target_kind` (none/member/message) → reply-binding rule (§7 step 4). Applies to reads too (`audit.get_actions_for_member`, `member.get_info/get_warnings` are member-kind reads).
- `input_schema` (Pydantic, admin) → `validate_params()`; `schema_dict()` = Pydantic wins → legacy dict. Chat tools: `input_schema=None` → `validate_params` raises `InvalidToolInput` by contract — so chat params are validated lightly against legacy `parameters` and passed to `dispatch_async`, never through `validate_params`.
- `ai_selectable` — admin: all 54 True → planner allowlist = `ai_catalog()` exactly. Chat: all False (irrelevant; chat visibility is by `category`, not this flag).
- `human_verb` (nullable; many reads None) → confirmation preview + outcome labels, fallback to `tool.name`.
- `description/parameters` — planner prompt + chat function definitions.

Interface signatures (frozen names; B/E implement):
```python
def propose_plan(req: HarnessRequest) -> Plan: ...
def validate_plan(plan: Plan, catalog) -> ValidatedPlan: ...  # raises PlanRejected
def authorize_step(step, ctx: AgentContext, tool) -> AuthVerdict: ...
def classify_access(tool) -> Literal["read","write"]: ...  # read ⟺ risk==low AND requires_confirmation==False; else write (covers enable_* trio)
def run_workflow(validated: ValidatedPlan, ctx, budget) -> HarnessOutcome: ...
def observe(result: StepResult, state) -> ObserverDecision: ...  # CONTINUE|WAIT_CONFIRM|REPLAN|FINISH|FAIL
```

## 4. Plan schema (FINAL, `harness.plan/v1`)

```python
class PlanStep(BaseModel, extra="forbid"):
    id: str                        # ^[a-z0-9_]{1,32}$, unique in plan
    tool: str                      # chat 5 | ai_catalog 54 | "run_code" — nothing else
    params: dict[str, Any] = {}    # admin: validated via tool.validate_params(); chat: legacy-shape checked, dispatch_async; run_code: {code: str ≤4000}
    target_source: Literal["reply","none"] = "none"  # member/message-kind steps MUST declare "reply" (value bound at execute-time, never from planner text)
    depends_on: list[str] = []     # DAG edges; refs must exist; cycles rejected
    on_failure: Literal["abort","skip","replan"] = "abort"
    timeout_s: int = 15            # 1..15, sandbox ops 2s inside
    needs_evidence: bool = False   # research steps must return sources (C6 evidence)

class Budget(BaseModel, extra="forbid"):
    max_tool_calls: int = 12       # 1..12
    max_runtime_s: int = 90        # 1..90, confirm-wait excluded

class Plan(BaseModel, extra="forbid"):
    version: Literal["harness.plan/v1"]
    goal: str                      # 1..500
    steps: list[PlanStep]          # 1..MAX_STEPS (8)
    budget: Budget
```

Deterministic validation (B, no LLM), codes:
V1 version literal. V2 1≤steps≤8; V3 ids unique+pattern. V4 `depends_on` exist + acyclic (topo-sort; order frozen into `ready_queue`). V5 tool allowlisted AND catalog-visible for caller class (chat-origin runs cannot name admin tools; admin tools require `registry.has()` + `ai_selectable`; chat steps require membership in chat 5; `run_code` allowed). V6 params: admin → `tool.validate_params()` must pass (drops None like orchestrator); chat → required legacy keys present; `run_code` → `code` 1..4000. V7 target-shape: `target_kind` member/message → `target_source=="reply"` declared (covers member/message reads); binding happens at execute-time from `AgentContext.reply`, else `AmbiguousTarget` deny. V8 write steps (`classify_access==write`) require `on_failure != "skip"` unless a fallback step exists. V9 budget within global caps. V10 no `harness.investigate` inside its own expansion (preserve existing guard; investigate used as a step only, never nested). V11 timeout/size fields within caps. V12 `needs_evidence` steps must be research/chat-fetch class (web_search/fetch_url/harness.investigate/analytics reads) — privileged writes cannot satisfy evidence. Failure → `PlanRejected{code, step_id?, reason}`.

## 5. HarnessRun / HarnessStep contract (FINAL)

Phase 1: in-memory runtime only, no DB migration. Durability via existing `AgentAuditLog` + `AgentConfirmation` rows. **Storage abstraction required**: E must define `RunStore` protocol (ABC/Protocol class with `save_run, load_run, update_step, mark_terminal`) so a persistent backend (DB, Redis) can replace the in-memory dict without changing executor/observer. Phase 1 implementation: `InMemoryRunStore`. Process restart during a run → run lost; on next interaction, harness must report `FAILED` with `"run_state_lost"` error code — never silently claim success or resume a corrupted run.

```python
class HarnessStepState:
    step: PlanStep
    status: PENDING|RUNNING|WAITING_CONFIRMATION|COMPLETED|FAILED|SKIPPED|DENIED
    verdict: AuthVerdict | None     # set by Security Gate, immutable after
    result_ref: str                # truncated ≤MAX_RESULT_SIZE + evidence list (C6)
    attempts: int                  # confirm-resume does not increment; replan creates new validated plan
class HarnessRunState:
    run_id: str                    # uuid4 hex (matches orchestrator request_id shape), attached to audit context via §11 OPEN-1 encoding
    status: PENDING|RUNNING|WAITING_CONFIRMATION|COMPLETED|FAILED|CANCELLED|PARTIAL
    steps: dict[str, HarnessStepState]
    ready_queue: list[str]         # topo order from V4
    tool_calls_used: int           # ≤ MAX_TOOL_CALLS (12)
    started_at: datetime; deadline_at: datetime  # deadline = started + max_runtime_s; confirm-wait excluded (clock paused while WAITING_CONFIRMATION)
    replan_count: int              # ≤ MAX_REPLANS (2)
    confirmation_ref: str | None   # token_hash only, never raw token
```

Result passing: step N output truncated into run memory; step N+1 references `{"from_step": "<id>", "field": "<key>"}` resolved by Executor with size cap — never raw string-concat of external text into privileged params. External excerpts flow as `untrusted` evidence, blocked from `target_user_id/message_id/chat_id/target` fields (those accept only reply-bound or allowlisted values). `AgentConfirmation.STATUS_*` (pending/confirmed/cancelled/expired/executing/executed/failed) remain the confirmation-row states; `WAITING_CONFIRMATION` is the run/step state while parked on a pending row.

## 6. State machine (FINAL)

```
PENDING → RUNNING ⇄ WAITING_CONFIRMATION → RUNNING → COMPLETED
   │          │                                      ╱  │  ╲
   │          ▼                                     ▼   ▼   ▼
   │       FAILED ──(re-plan → C2→C4 from scratch, bounded)──► RUNNING
   └─────► CANCELLED (user cancel / confirm expiry on blocking step / global deadline)   PARTIAL (≥1 COMPLETED, abort disclosed)
```

- PENDING: plan validated + first-step authorized, not started.
- RUNNING: sequential topo-order execution Phase 1 (no parallel tool calls — avoids confirmation/target races).
- WAITING_CONFIRMATION: parked on existing `AgentConfirmation` (TTL 120s, requester-only, UI-chat scoped via `request_chat_id`, atomic `claim_for_execution`). Callback confirmed → `execute_confirmed` (re-resolve identity no-cache + permission + capability + stored-params revalidation) → RUNNING. Cancelled/expired → step FAILED/DENIED → Observer abort/partial.
- COMPLETED: all steps COMPLETED (or policy-skipped with disclosure). FAILED: validation failure, non-skippable deny, timeout, replan-exhausted, unexpected exception (logged w/ request_id, generic user text). CANCELLED: as above. PARTIAL: aborted with ≥1 COMPLETED, honest report. Never claim full success on partial.
- Guards: re-planned steps re-enter C2→C4 (no Security skip). Observer cannot force COMPLETED. Max one active confirmation per run. Stuck `executing` rows never auto-retried (`fail_stuck_executing` surfaces only).

## 7. Security Gate — exact reuse (no reimplementation)

Per step, in this order (same functions, same order as `orchestrator.handle_admin_command` + `execute_confirmed`):
1. Resolve: `resolve_target_chat` (operational chat + cleaned command) → `resolve_admin_identity` (Telegram role; `ADMIN_IDS` override; 30s cache; `use_cache=False` on confirm-resume) → `resolve_bot_capabilities`. Build `AgentContext` (`context.py`; AI sees only `ai_payload()` redacted).
2. Tool resolve: admin → `registry.get` (raises `UnknownTool` otherwise); chat → chat-definitions membership (never `get()`); `run_code` → sandbox pseudo. Confidence gates stay: single-step AI parse ≥0.80 (`AGENT_MIN_CONFIDENCE`), harness fallback ≥0.5 LOW-only (`orchestrator._decide`).
3. Channel guard: `tool.name in _CHANNEL_UNSUPPORTED_TOOLS` (8: member.mute/unmute/ban/unban/warn/get_warnings, group.lock/unlock) + `ctx.is_channel` → deny `channel_unsupported`.
4. Target binding: `_resolve_target` verbatim — member-kind: reply.user_id overwrites anything planner sent, missing → `AmbiguousTarget`; message-kind: reply.message_id, missing → `AmbiguousTarget`. Planner-supplied ids never trusted. No fabricated ids (Target Safety).
5. Param validation: drop None values → `tool.validate_params()` (admin). Failure → `invalid_tool_input` deny.
6. Permission + capability: `ensure_admin_permission(ctx.admin, tool.permission)` (role map `PERMISSION_MIN_ROLE`: viewer 6 read families, moderator warn/restrict/delete/pin/post, administrator ban/bulk_delete/settings.write) → `ensure_bot_capability(ctx.bot_capabilities, tool.capability)` (4 caps). Both must pass.
7. Protected target: `_ensure_target_allowed` for `PROTECTED_ACTION_TOOLS={member.mute, member.ban, member.warn}` (bot-self + creator/administrator → `protected_target`; lookup-error fail-open, capability guard still protects).
8. Risk + confirmation: `risk = resolve_risk(tool.risk_level, ai_risk)` (registry wins, only raise); `requires_confirmation = tool.requires_confirmation or risk==HIGH`. If true → `create_confirmation` (TTL 120, `token_hash` stored, raw only in `callback_data` ≤46 bytes) + `confirmation_preview` + audit `awaiting_confirmation` → verdict NEED_CONFIRMATION, run parks. Else verdict ALLOW with audit `started`.
9. Execute-time revalidation (every privileged step AND confirm-resume): `registry.get` again + `resolve_admin_identity(use_cache=False)` + permission + capability + stored-params `validate_params` (immutable) — inherits `execute_confirmed`. Confirm lifecycle: atomic `claim/cancel`, ownership + UI-chat + expiry checks, `VALID_TRANSITIONS`, `mark_executed/mark_failed`, stuck never retried.
10. Trust boundaries: external outputs stay `<external_source>`/UNTRUSTED DATA (evidence.py) end-to-end — cannot name tools, set targets, grant permission, skip confirmation, or inject memory-as-instruction. Memory stays `DATA:`-wrapped (`memory/integration.py`), never auth/directive. Credentials never in prompts/logs/audit (`sanitize_parameters`: token/secret/key/authorization/password/cookie/seed → `[REDACTED]`, str ≤200). Session/cookie behind Connector → Credential Store; AI sees results only. `run_code` only `run_sandboxed` (AST denylist, `__`/import/os./sys./subprocess/socket blocked, 2s timeout, 4000 cap, JSON-serializable `result` only). Dual-catalog isolation + fail-open routing (`classify.py`, Jev fast-path 0.80/0.70, route threshold 0.72) preserved.
11. Audit every branch via existing `record_audit` (pending/started/awaiting_confirmation/executed/failed + error codes). No silent harness execution.

Read/write rule (locked): `classify_access(tool) = "read" ⟺ (risk_level==low AND requires_confirmation==False)`, else `"write"`. Yields 26 read / 28 write over the 54 — including the `settings.enable_*` trio (medium/no-confirm) correctly classified write. Replaces fragile `_WRITEISH_PREFIXES` string matching prospectively (existing `harness.py` untouched Phase 1; cutover via CACR in Phase 4).

## 8. Integration points (FINAL)

| Touchpoint | File | Integration (G applies; B–F via CACR) |
|---|---|---|
| Entry predicate | `agent_handlers.py` AgentTriggerFilter + `classify.py` | new multi-step predicate AFTER single-step deterministic parse; thresholds unchanged (0.72 route, 0.80 Jev-agent/0.70 Jev-chat); fail-open to chat; no dispatcher reorder |
| Chat path | `services.py call_noya_api` (`NOYA_API_TIMEOUT` 45), `agent/loop.py` (`MAX_ITERATIONS` 8, searches 3, fetches 6, `chat_definitions` only) | untouched; harness research uses same `WebService`/chat tools without stealing live turns |
| Admin path | `agent/orchestrator.py` (`handle_admin_command`, `execute_confirmed`, order §7) | harness writes delegate here; no direct handler imports |
| Registry | `agent/registry.py` (dual-catalog, strict `register_tool`), `agent/tools.py` (5 chat) | harness read-only (`ai_catalog`, `chat_definitions`, `all_tools` for branching, `validate_params`, `dispatch_async`); D deltas as data patch via G |
| Existing investigate | `agent/harness.py run_investigation` | one step type; recursion guard + LOW-only auto-run kept |
| Confirm/audit/errors | `confirmations.py` (TTL 120, hash-only, atomic claim), `callbacks.py`, `audit_log.py` (sanitized), `errors.py` (codes) | reused verbatim; run/step correlation via OPEN-1 |
| Permissions/risk/target/context | `permissions.py` (14 perms, 4 caps, role cache 30s), `risk.py` (`RISK_ORDER`, resolve max), `target_chat.py`, `context.py` (`ai_payload` redacted, excerpt 240) | wrapped, not forked |
| Memory | `memory/*.py` (extraction/storage/retrieval/privacy/context/integration), `ai/prompts.py` (memory untrusted) | existing retrieval/context builders; DATA wrapper kept |
| Web/research | `web/service.py`, `extractor.py`, `cleaner.py`, `ranking.py`, `evidence.py` (UNTRUSTED block), `security.py` (SSRF, anti-rebinding), `fetcher.py` (5 redirects, 5MB, 5/12/18s), `policy.py` | reused; all harness fetches under same guards |
| Moderation | `moderation.py` (`sys:`/`ext:`), `telegram_moderation.py`, `runbot.py` pre-chat gate | never bypassed; mod tools only via admin path |
| Response | `agent/responses.py`, runbot/handlers send | outcome rendering only |
| Config | `config/settings.py` | new keys (names only): `HARNESS_CORE_ENABLED, HARNESS_MAX_REPLANS, HARNESS_MAX_TOOL_CALLS, HARNESS_TOTAL_RUNTIME_S, HARNESS_RESULT_CAP`; existing keys unchanged; no secrets |
| Models | `models.py` | NO migration Phase 1 (`AgentConfirmation` 7 states + `token_hash` unique + `request_chat_id`; `AgentAuditLog` sanitized) |

Pre-Phase-2 requirement: G commits Registry P0 baseline (`registry.py`, `tools.py`, `loop.py`, `test_registry.py`) so B–F build against frozen tree.

## 9. Parallel execution plan (GATE ENFORCED)

- Phase 0 DONE. Phase 1 = this file (gate). No code.
- Phase 2 parallel ONLY after written approval: B schemas/validation/planner | C gate/classifiers (no registry edits) | D matrix (no registry edits) | E executor/observer/state/limits (new package only) | F simulation tests (mocks/fakes).
- Phase 3 G review (compat, conflicts, duplicate-reject, CACR apply). Phase 4 Integration (G only). Phase 5 Regression/security (`manage.py check`, targeted + full suite, F adversarial: permission failure, malicious evidence, credential isolation, replan limit, admin-tool isolation, multi-step analytics/research). Phase 6 Final report (What I Found / Changed / Files Changed / Tests / Verification / Remaining / Architecture Impact).

## 10. Files planned + Limits (FINAL, derived)

New (Phase 2+): `botapp/harness/__init__.py, schemas.py, plans.py, validation.py, planner.py, security.py, capabilities.py, executor.py, observer.py, state.py, limits.py, errors.py`; `botapp/tests/test_harness_plan.py, test_harness_security.py, test_harness_executor.py, test_harness_adversarial.py`; `HARNESS_ARCHITECTURE.md` (A); capability matrix doc (D).
Modified (G only): `agent_handlers.py` (entry predicate), `config/settings.py` (5 keys), docs deltas; `agent/harness.py` cutover to structured classification only via ACR+CACR (default: untouched).

| Limit | Value | Derived from |
|---|---|---|
| MAX_STEPS | 8 | `AGENT_HARNESS_MAX_STEPS=8` (settings:136) |
| Default plan steps | ≤5 | `MAX_STEPS_DEFAULT=5` (harness.py:28) |
| MAX_REPLANS | 2 | new, bounded well below loop `MAX_ITERATIONS=8` |
| MAX_TOOL_CALLS | 12 | steps 8 + replan margin; prevents fan-out |
| MAX_TOTAL_RUNTIME_S | 90 | 2× `NOYA_API_TIMEOUT=45` (services.py:25); confirm-wait excluded (TTL 120 governs) |
| Per-tool timeout | 15 | new; under NOYA 45, above sandbox 2.0 |
| Sandbox timeout/chars | 2.0s / 4000 | `run_sandboxed` defaults (sandbox.py) — unchanged |
| MAX_RESULT_SIZE | 4000/step | sandbox `max_chars`; existing observation 3500 stays inside |
| Evidence | ≤500/source, ≤6 sources | C6; inside result cap |
| Confirmation TTL | 120s | `AGENT_CONFIRMATION_TTL_SECONDS` (settings:130, orchestrator:262) |
| AI confidence | 0.80 single / 0.50 harness-fallback | `AGENT_MIN_CONFIDENCE` + `_decide` fallback (orchestrator:120-130) |
| Command length | ≤2000 | `AGENT_MAX_COMMAND_LENGTH` (settings:132, orchestrator:198) |
| Execution | sequential | avoids confirmation/target races Phase 1 |

## 11. Decisions logged (APPROVED — gate closed)

- DECISION-1 Audit correlation: `request_id` format `harness:<run_id>:<step_id>` via existing `record_audit`. No schema change, no migration Phase 1.
- DECISION-2 Package path: `botapp/harness/` (top-level under `botapp/` to prevent `botapp/agent/` edit collisions).
- DECISION-3 Entry predicate: only genuine multi-step/investigation requests. Existing single-tool admin commands remain on current Orchestrator. Threshold >= 0.80 conservative default unless classifier semantics dictate otherwise.
- DECISION-4 Storage: Phase 1 `InMemoryRunStore` implementing `RunStore` protocol (ABC/Protocol: save, load, update_step, mark_terminal). Restart resume is not guaranteed and must fail honestly with `run_state_lost`.
- DECISION-5 Limits locked: table §10 values frozen. Web/Research tools use existing per-call timeouts if needed, bounded by global `MAX_TOTAL_RUNTIME_S=90`.
- DECISION-6 Classification: Harness Core must NOT use `_WRITEISH_PREFIXES`. Authorization/classification uses structured Registry metadata (`risk_level == low and requires_confirmation == False`). Legacy `harness.py` untouched Phase 1.
- DECISION-7 Budget & target: `Budget{max_tool_calls: int, max_runtime_s: int}` and `target_source: Literal["reply", "none"]`. Planner must never invent user_id/message_id/chat_id targets.
- DECISION-8 Sandbox: `run_code` is internal sandbox-only in Phase 1 (`sandbox.run_sandboxed`), not an AI-selectable external tool. No network, filesystem side effects or privileged execution.

## 12. Readiness

- DESIGN_GATE: APPROVED
- CONTRACTS_FROZEN: YES (C1–C9 + signatures + V1–V12 + decisions 1–8 locked)
- OWNERSHIP_READY: YES (§2 with `test_registry.py` → G; dirty baseline commit required pre-Phase-2)
- REGISTRY_COMPATIBLE: YES (59/54/5 verified; §3 mapping covers all fields)
- Phase 2 implementation: NOT started in this chat (stopped per instructions)
