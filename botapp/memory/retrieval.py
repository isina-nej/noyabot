from django.db.models import Q
from django.utils import timezone

from botapp.models import MemoryConversation, MemoryItem
from .privacy import is_retrievable


def _terms(query: str):
    return [term for term in " ".join((query or "").casefold().split()).split() if len(term) > 1]


def retrieve_memories(user_id: int, conversation: MemoryConversation, query: str, limit: int = 20):
    now = timezone.now()
    queryset = MemoryItem.objects.select_related("owner_user").filter(
        owner_user__telegram_user_id=user_id,
        status=MemoryItem.Status.ACTIVE,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now) | Q(retention_level=MemoryItem.Retention.CRITICAL))
    terms = _terms(query)
    scored = []
    for item in queryset:
        if not is_retrievable(item, user_id, conversation.id, conversation.chat_type):
            continue
        haystack = f"{item.content} {item.category}".casefold()
        score = sum(1 for term in terms if term in haystack)
        # Explicit important facts should remain recallable despite synonyms.
        important = item.is_explicit or item.importance in {"critical", "important"}
        if score or not terms or important:
            score = score * 100
            score += {"critical": 90, "important": 45, "moderately_important": 10, "unimportant": 0}.get(item.importance, 0)
            score += 10 if item.is_explicit else 0
            score += 2 if item.conversation_id == conversation.id else 0
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
    items = [item for _, item in scored[:max(limit, 0)]]
    if items:
        MemoryItem.objects.filter(pk__in=[item.pk for item in items]).update(
            last_accessed_at=now,
        )
    return items
