from dataclasses import dataclass

from .retrieval import retrieve_memories


@dataclass(frozen=True)
class MemoryContext:
    text: str
    token_count: int
    items: tuple


def _token_count(text: str) -> int:
    return len(text.split())


def build_memory_context(user_id, conversation, query, max_tokens=600, limit=20):
    items = retrieve_memories(user_id, conversation, query, limit=limit)
    selected = []
    total = 0
    for item in items:
        # Preserve the memory category so relationship notes can tune warmth
        # and trust without being mistaken for ordinary user preferences.
        line = f"- [{item.category}] {item.content}"
        cost = _token_count(line)
        if total + cost > max(max_tokens, 0):
            continue
        selected.append(item)
        total += cost
    return MemoryContext(
        text="\n".join(f"- [{item.category}] {item.content}" for item in selected),
        token_count=total,
        items=tuple(selected),
    )
