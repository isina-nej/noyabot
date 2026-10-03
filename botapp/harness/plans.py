"""DAG representation + deterministic topological ordering.

Pure: stdlib only. No Django, no Registry, no execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import PlanStep


def validate_dag(steps: list[PlanStep]) -> None:
    """Raise PlanRejected on duplicate/unknown/self/cyclic/finish misuse."""
    from .errors import PlanRejected

    if not steps:
        raise PlanRejected(code="empty_plan")
    ids = [s.id for s in steps]
    seen: set[str] = set()
    for sid in ids:
        if sid in seen:
            raise PlanRejected(code="duplicate_step_id", step_id=sid)
        seen.add(sid)
    known = set(ids)
    for step in steps:
        for dep in step.depends_on:
            if dep == step.id:
                raise PlanRejected(code="self_dependency", step_id=step.id)
            if dep not in known:
                raise PlanRejected(code="unknown_dependency", step_id=step.id)
    # Cycle check via deterministic order attempt.
    topological_order(steps)
    finishes = [s for s in steps if s.action == "finish"]
    if len(finishes) > 1:
        raise PlanRejected(code="multiple_finish", step_id=finishes[1].id)
    if finishes:
        terminal = {finishes[0].id}
        for step in steps:
            if step.action != "finish" and terminal & set(step.depends_on):
                raise PlanRejected(code="finish_not_terminal", step_id=step.id)


def topological_order(steps: list[PlanStep]) -> list[PlanStep]:
    """Kahn's algorithm, ready queue sorted by id. Raises on cycle."""
    from .errors import PlanRejected

    by_id = {s.id: s for s in steps}
    indegree = {s.id: 0 for s in steps}
    children: dict[str, list[str]] = {s.id: [] for s in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep in by_id and dep != step.id:
                indegree[step.id] += 1
                children[dep].append(step.id)
    ready = sorted([sid for sid, deg in indegree.items() if deg == 0])
    order: list[PlanStep] = []
    while ready:
        sid = ready.pop(0)
        order.append(by_id[sid])
        for child in children[sid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()
    if len(order) != len(steps):
        raise PlanRejected(code="cycle_detected")
    return order
