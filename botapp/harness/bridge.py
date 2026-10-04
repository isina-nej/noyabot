"""Application bridge: harness.investigate -> new botapp/harness/* chain.

Entry: botapp/agent_tools/harness.py :: investigate (sole production edit).
Chain per turn (legacy-compatible): planner step -> validate_raw_step ->
authorize_harness_step -> HarnessExecutor single-step arun -> store.save ->
Observer verdict. run_code via agent sandbox only. finish returns V10 text,
other failures use _fallback_answer.

Request-wide budgets (botapp/harness/limits.py) are shared by the whole
request, never reset per turn: total runtime 90s, step cap (max 8),
tool-call cap 12, replan cap 2, per-step timeout <=15s, result size 4000
(enforced inside the executor/state truncation helpers). One request-level
run in the RunStore owns every turn, so history accumulates instead of
keeping only the last single-step execution.

Plan semantics: per-turn decisions use validate_raw_step (V1-V3, V6-V8,
V10 structural checks + self-call refusal). Full-plan checks
(kind/budget/allowlist/DAG: duplicate/unknown/self/cycle/finish-terminal)
live in validate_plan and guard the deterministic full-plan path; per-turn
ordering is turn order, not a caller-supplied DAG (depends_on on a single
step carries no cross-turn edges). Allowlist (V4) + dual-gate (V5) are
enforced per turn by authorize_harness_step, inside the executor too.

Frozen behavior preserved (C1-C9, V1-V12, §5/§6, §10, §14, §15):
- AGENT_HARNESS_ENABLED=false -> ok=False, error="harness_disabled".
- AI off (or planner failure at step 1 with no observations) ->
  deterministic §3 pass, error="deterministic" + provenance footer.
- Invalid steps rejected BEFORE execution (note + continue, handler untouched).
- authorize_harness_step NEED_CONFIRMATION -> DENY; params forced {} (V6).
- Reply is the target truth (inside authorize); research stays UNTRUSTED.
- No privileged handler runs without an ALLOW verdict + validated params.
- run_code executes only via botapp.agent.sandbox (V8); finish = V10 text.
- ctx, bot, run_id, Observer verdict and RunStore capture preserved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

logger = logging.getLogger("botapp.agent")

_store = None
_last_result = None


def get_bridge_store():
    """Process-local store holding application-triggered runs (observable)."""
    global _store
    if _store is None:
        from botapp.harness.state import InMemoryRunStore

        _store = InMemoryRunStore(max_runs=200)
    return _store


def reset_bridge_store():
    """Tests only: drop the bridge store and last result."""
    global _store, _last_result
    _store = None
    _last_result = None


def get_last_bridge_result():
    """Tests only: last BridgeResult produced through this bridge."""
    return _last_result


def _setting(name: str, default):
    return getattr(settings, name, default)


@dataclass
class BridgeResult:
    ok: bool
    answer: str
    steps_used: int = 0
    observations: list = field(default_factory=list)
    error: str = ""
    run_id: str = ""
    run_status: str = ""
    observer_verdict: str = ""


def _obs(kind: str, name: str, summary: str, data: Any = None):
    from botapp.agent.harness import Observation

    return Observation(kind=kind, name=name, summary=summary, data=data)


def _det_payload(allowlist: list[str], cap: int):
    """Filtered deterministic §3 plan (skip unregistered tools, rechain DAG)."""
    from botapp.harness.planner import deterministic_plan

    planned = deterministic_plan(max_steps=cap)
    kept = [s for s in planned.plan.steps if s.action != "call_tool" or s.tool in allowlist]
    raw = []
    prev = None
    for i, s in enumerate(kept):
        sid = f"s{i + 1:02d}"
        d = s.model_dump()
        d["id"] = sid
        d["depends_on"] = [prev] if prev else []
        raw.append(d)
        prev = sid
    return raw, planned.error


async def run_application_investigation(
    *,
    ctx,
    bot,
    user_text: str,
    max_steps: int | None = None,
    step_planner=None,
    store=None,
) -> BridgeResult:
    """Run the new chain for a real application context.

    step_planner: optional single-step planner like legacy ask_harness_step
    (tests/AI). Internal validation/security/execution always real.
    """
    global _last_result
    from botapp.agent.harness import (
        MAX_OBSERVATION_CHARS,
        _fallback_answer,
        _format_memory,
        _observations_payload,
        _truncate,
    )
    from botapp.harness.errors import PlanRejected
    from botapp.harness.executor import HarnessExecutor
    from botapp.harness.limits import (
        MAX_STEPS,
        MAX_TOOL_CALLS,
        MAX_TOTAL_RUNTIME_S,
        PER_STEP_TIMEOUT_S,
    )
    from botapp.harness.observer import Observer
    from botapp.harness.schemas import resolve_max_steps
    from botapp.harness.security import (
        AuthVerdict,
        authorize_harness_step,
        build_harness_allowlist,
    )
    from botapp.harness.state import FINISHED, create_run, new_run_id
    from botapp.harness.validation import validate_plan, validate_raw_step

    def _done(**kw):
        global _last_result
        res = BridgeResult(**kw)
        _last_result = res
        return res

    if not _setting("AGENT_HARNESS_ENABLED", True):
        return _done(ok=False, answer="❌ بررسی مرحله‌ای غیرفعال است.", error="harness_disabled")

    try:
        cap = resolve_max_steps(_setting("AGENT_HARNESS_MAX_STEPS", 8) if max_steps is None else max_steps)
    except PlanRejected:
        cap = 5
    # Request step limit: maximum MAX_STEPS (8). The deterministic §3 chain
    # below is fixed-length and may extend cap to fit itself (frozen planner
    # behavior); the AI/planner turn loop never exceeds MAX_STEPS.
    cap = min(cap, MAX_STEPS)
    use_ai = bool(_setting("AGENT_AI_ENABLED", True)) and bool(_setting("AGENT_HARNESS_AI_ENABLED", True))

    allowlist = build_harness_allowlist()
    active = store or get_bridge_store()
    observations: list = []

    if step_planner is not None:
        planner, ai_mode = step_planner, True
    elif use_ai:
        from botapp.agent.ai import ask_harness_step as _ask

        planner, ai_mode = _ask, True
    else:
        planner, ai_mode = None, False

    # One request-level run owns every turn. Counters here are the shared
    # request budgets; each single-step executor is constructed with the
    # *remaining* budget so per-turn execution cannot reset them.
    # Wall-clock covers planner + sandbox + tools; the executor additionally
    # enforces per-step timeout and result-size truncation per execution.
    request_run = create_run([], run_id=new_run_id())
    active.create(request_run)
    request_run_id = request_run.run_id
    request_tool_calls = 0
    request_start = None
    try:
        import time as _time

        request_start = _time.monotonic()
    except Exception:  # pragma: no cover - monotonic always available
        request_start = None

    def _request_elapsed() -> float:
        if request_start is None:
            return 0.0
        try:
            import time as _time

            return max(0.0, _time.monotonic() - request_start)
        except Exception:  # pragma: no cover - defensive
            return 0.0

    def _sync_request_run(status: str = "") -> None:
        run = active.get(request_run_id)
        if run is None:  # pragma: no cover - defensive
            return
        run.tool_calls_used = request_tool_calls
        run.active_runtime_s = _request_elapsed()
        if status:
            run.status = status
        active.save(run)

    def _finalize():
        """Request-level identity + observer verdict for terminal returns."""
        _sync_request_run(status=FINISHED)
        run = active.get(request_run_id)
        verdict = Observer().observe(run).verdict if run is not None else ""
        return request_run_id, FINISHED, verdict

    det_steps, det_idx, plan_error = None, 0, ""
    if not ai_mode:
        raw, plan_error = _det_payload(allowlist, cap)
        try:
            checked = validate_plan(
                {"kind": "harness.plan/v1",
                 "budget": {"max_steps": max(cap, len(raw))},
                 "steps": raw},
                allowlist=allowlist,
                max_steps=max(cap, len(raw)),
                allow_empty_finish=True,
            )
            det_steps = checked.steps
            for note in checked.notes:
                observations.append(_obs("note", note.step_id, f"{note.code}: {note.summary}"))
        except PlanRejected as exc:  # pragma: no cover - defensive
            observations.append(_obs("note", "denied", exc.user_message))
            return _done(ok=True, answer=exc.user_message, observations=observations, error=exc.code,
                         run_id=request_run_id)

    async def gated(tool_name: str, params: dict):
        decision = await authorize_harness_step(ctx, tool_name, bot=bot, allowlist=allowlist)
        if decision.verdict != AuthVerdict.ALLOW:
            raise PermissionError(f"{decision.reason_code}: {decision.user_message}")
        from botapp.agent.registry import registry

        tool = registry.get(tool_name)
        model = tool.validate_params(decision.validated_params or {})
        return str(await tool.handler(ctx, bot, model) or "")

    def _run_code_obs(code: str):
        from botapp.agent.sandbox import run_sandboxed

        res = run_sandboxed(code, data=_observations_payload(observations))
        if not res.ok:
            return _obs("code", "run_code", f"SandboxError: {res.error}")
        import json as _json

        out: dict[str, Any] = {"result": res.result}
        if res.stdout:
            out["stdout"] = res.stdout
        try:
            summary = _json.dumps(out, ensure_ascii=False, default=str)
        except Exception:
            summary = str(out)
        return _obs("code", "run_code", _truncate(summary, MAX_OBSERVATION_CHARS), data=out)

    async def _exec_tool(step_id: str, tool_name: str):
        """Execute one turn against the *remaining* request budget.

        Returns (stored, sstate, verdict); sstate None means the shared
        request budget was already exhausted, so nothing executed.
        """
        nonlocal request_tool_calls
        if request_tool_calls >= MAX_TOOL_CALLS:
            return None, None, "BUDGET_STOP"
        remaining_runtime = MAX_TOTAL_RUNTIME_S - _request_elapsed()
        if remaining_runtime <= 0:
            return None, None, "BUDGET_STOP"
        runner = HarnessExecutor(
            store=active,
            tool_executor=gated,
            max_steps=1,
            max_tool_calls=1,
            max_total_runtime_s=remaining_runtime,
            step_timeout_s=min(PER_STEP_TIMEOUT_S, remaining_runtime),
        )
        erun = runner.create_run([{"step_id": step_id, "tool": tool_name, "params": {}}])
        final = await runner.arun(erun.run_id)
        stored = active.get(erun.run_id)
        sstate = stored.steps[0] if stored and stored.steps else None
        # Accumulate this turn into the request-level run instead of keeping
        # the one-step executor run as the request history.
        request_state = active.get(request_run_id)
        if request_state is not None and sstate is not None:
            request_state.steps.append(sstate)
            request_state.current_index = len(request_state.steps)
            active.save(request_state)
        request_tool_calls += 1
        _sync_request_run()
        verdict = Observer().observe(active.get(request_run_id) or final).verdict
        return stored, sstate, verdict

    _sync_request_run(status="running")
    det_exhausted = False
    # Deterministic §3 chain is fixed length; the turn budget must fit it
    # (legacy loop runs observations only, cap applies to AI turns).
    det_mode = not ai_mode
    if det_mode:
        cap = max(cap, len(det_steps))
    turn = 0
    while turn < cap:
        turn += 1
        if det_mode:
            if det_idx >= len(det_steps):
                det_exhausted = True
                break
            s = det_steps[det_idx]
            det_idx += 1
            decision = {
                "action": s.action, "tool": s.tool, "args": {},
                "code": s.code, "answer": s.answer, "thinking": s.thinking,
            }
        else:
            try:
                maybe = planner(
                    user_text=user_text, memory=_format_memory(observations),
                    allowed_tools=allowlist, chat_id=ctx.chat_id, step=turn, max_steps=cap,
                )
                import inspect as _inspect

                decision = await maybe if _inspect.isawaitable(maybe) else maybe
            except Exception:
                logger.exception("harness planner failed chat=%s step=%s", ctx.chat_id, turn)
                if observations:
                    rid, rs, rv = _finalize()
                    return _done(ok=True, answer=_fallback_answer(observations), steps_used=turn,
                                 observations=observations, error="llm_step_failed",
                                 run_id=rid, run_status=rs, observer_verdict=rv)
                raw, plan_error = _det_payload(allowlist, cap)
                try:
                    checked = validate_plan(
                        {"kind": "harness.plan/v1",
                         "budget": {"max_steps": max(cap, len(raw))},
                         "steps": raw},
                        allowlist=allowlist, max_steps=max(cap, len(raw)), allow_empty_finish=True,
                    )
                    det_steps, det_idx, det_mode = checked.steps, 0, True
                    turn -= 1
                    continue
                except PlanRejected as exc:  # pragma: no cover - defensive
                    observations.append(_obs("note", "denied", exc.user_message))
                    return _done(ok=True, answer=exc.user_message, observations=observations, error=exc.code,
                                 run_id=request_run_id)

        action = (decision.get("action") or "").strip().lower()
        if action == "finish":
            answer = (decision.get("answer") or "").strip()
            if not answer and det_mode:
                plan_error = plan_error or "deterministic"
                answer = _fallback_answer(observations)
            else:
                answer = answer or _fallback_answer(observations)
            rid, rs, rv = _finalize()
            if det_mode and not (decision.get("answer") or "").strip():
                answer = (f"{answer}\n\n——\nدرخواست: {_truncate(user_text, 200)}\n"
                          "این بررسی بدون برنامه‌ریز LLM و فقط با ابزارهای خواندنی انجام شد.")
                return _done(ok=True, answer=answer, steps_used=turn, observations=observations,
                             error=plan_error or "deterministic",
                             run_id=rid, run_status=rs, observer_verdict=rv)
            return _done(ok=True, answer=answer, steps_used=turn, observations=observations, error="",
                         run_id=rid, run_status=rs, observer_verdict=rv)

        try:
            # Single-step check only: V1-V3/V6-V8/V10 structure + self-call
            # refusal. Full-plan checks (kind/budget/allowlist/DAG) do not
            # apply to one turn; per-turn order is turn order. V4 allowlist
            # and V5 dual-gate are enforced by authorize_harness_step below
            # (pre-check) and again inside the executor (gated).
            step = validate_raw_step(decision, step_id=f"{turn:03d}")
        except PlanRejected as exc:
            if exc.code == "empty_answer" and det_mode:
                plan_error = plan_error or "deterministic"
                answer = (f"{_fallback_answer(observations)}\n\n——\nدرخواست: {_truncate(user_text, 200)}\n"
                          "این بررسی بدون برنامه‌ریز LLM و فقط با ابزارهای خواندنی انجام شد.")
                rid, rs, rv = _finalize()
                return _done(ok=True, answer=answer, steps_used=turn, observations=observations,
                             error=plan_error or "deterministic",
                             run_id=rid, run_status=rs, observer_verdict=rv)
            code = exc.code
            observations.append(_obs("note", "code" if code == "empty_code" else "denied", exc.user_message))
            continue

        if step.action == "run_code":
            observations.append(_run_code_obs(step.code))
            continue

        pre = await authorize_harness_step(ctx, step.tool, bot=bot, allowlist=allowlist)
        if pre.verdict != AuthVerdict.ALLOW:
            observations.append(_obs("note", "denied", pre.user_message or "❌ این ابزار در بررسی مرحله‌ای مجاز نیست."))
            continue
        _stored, sstate, verdict = await _exec_tool(f"{turn:03d}", step.tool)
        if sstate is None:
            observations.append(_obs("note", "denied", "سقف بودجه درخواست تمام شد؛ گام اجرا نشد."))
            continue
        if sstate.status == "finished":
            text = _truncate(str(sstate.result or ""), MAX_OBSERVATION_CHARS)
            observations.append(_obs("tool", step.tool, text, data=text))
        else:
            err = sstate.error or "denied"
            if err.startswith("PermissionError") or "denied" in err:
                observations.append(_obs("note", "denied", f"ابزار «{step.tool}» در harness مجاز نیست."))
            else:
                observations.append(_obs("tool", step.tool, f"خطا: {err}"))

    answer = _fallback_answer(observations) or "به سقف گام‌های بررسی رسیدم؛ داده‌های جمع‌شده را خلاصه کردم."
    rid, rs, rv = _finalize()
    if det_exhausted:
        answer = (f"{answer}\n\n——\nدرخواست: {_truncate(user_text, 200)}\n"
                  "این بررسی بدون برنامه‌ریز LLM و فقط با ابزارهای خواندنی انجام شد.")
        return _done(ok=True, answer=answer, steps_used=len(observations), observations=observations,
                     error=plan_error or "deterministic",
                     run_id=rid, run_status=rs, observer_verdict=rv)
    return _done(ok=True, answer=answer, steps_used=cap, observations=observations, error="max_steps",
                 run_id=rid, run_status=rs, observer_verdict=rv)
