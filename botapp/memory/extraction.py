from dataclasses import dataclass
import re

from botapp.models import MemoryItem


@dataclass(frozen=True)
class MemoryCandidate:
    content: str
    category: str
    importance: str = MemoryItem.Importance.IMPORTANT
    retention_level: str = MemoryItem.Retention.LONG
    visibility: str = MemoryItem.Visibility.CONVERSATION_ONLY
    confidence: float = 0.95
    is_explicit: bool = False
    memory_scope: str = MemoryItem.Scope.USER_CONVERSATION


_PATTERNS = (
    (re.compile(r"^(?:یادت باشه|به خاطر بسپار|یادآوری کن)\s+(.+)$", re.I), "explicit"),
    (re.compile(r"^(?:من|اسم من)\s+(?:را )?(.+)$", re.I), "identity"),
    (re.compile(r"^(?:ترجیح می‌دهم|ترجیح میدم|دوست دارم)\s+(.+)$", re.I), "preference"),
    (re.compile(r"^(?:من|کار من)\s+(?:یک )?(.+?)\s+هستم$", re.I), "identity"),
)

# Leading bot triggers / mentions so "نویا یادت باشه X" still extracts.
_TRIGGER_PREFIX = re.compile(
    r"^(?:(?:@?(?:nuyarobot|noyarobot|noya|nuya|noia|nuia)|نویا)\s+)+",
    re.I,
)
_TRAILING_MENTION = re.compile(r"(?:\s+@\w+)+$", re.I)

_SKIP_PREFIXES = (
    "/", "ترجمه", "ترجمه کن", "بازنویسی", "بازنویسی کن", "بررسی کن",
    "rewrite", "translate", "summarize", "خلاصه کن",
)

_RELATIONSHIP_MARKERS = (
    "دلخور", "ناراحتم", "خوشحالم", "بی‌احترامی", "بدرفتاری", "خوبی کرد",
    "کمک کرد", "اعتماد", "کینه", "بخشید", "بخشش", "عذرخواهی", "جبران",
    "رابطه", "رفتارش", "مهربان", "دوست دارم",
)


def normalize_content(text: str) -> str:
    return " ".join(text.casefold().strip().split())


def _prepare_text(text: str) -> str:
    clean = " ".join(text.strip().split())
    clean = _TRIGGER_PREFIX.sub("", clean).strip()
    return _TRAILING_MENTION.sub("", clean).strip()


def extract_candidates(
    text: str | None,
    *,
    is_command: bool = False,
    is_forwarded: bool = False,
    is_moderation: bool = False,
    operation: str | None = None,
) -> list[MemoryCandidate]:
    if not isinstance(text, str):
        return []
    clean = _prepare_text(text)
    if not clean or is_command or is_forwarded or is_moderation:
        return []
    if operation in {"translate", "rewrite", "summarize", "review"}:
        return []
    if clean.casefold().startswith(_SKIP_PREFIXES):
        return []

    for pattern, kind in _PATTERNS:
        match = pattern.match(clean)
        if not match:
            continue
        value = match.group(1).strip(" .،")
        value = _TRAILING_MENTION.sub("", value).strip(" .،")
        if len(value) < 2:
            return []
        normalized_value = normalize_content(value)
        if any(marker in normalized_value for marker in _RELATIONSHIP_MARKERS):
            category = "relationship"
        else:
            category = "preference" if kind in {"explicit", "preference"} else "identity"
        explicit_global = kind == "explicit"
        return [MemoryCandidate(
            content=value,
            category=category,
            visibility=(MemoryItem.Visibility.USER_GLOBAL if explicit_global else MemoryItem.Visibility.CONVERSATION_ONLY),
            memory_scope=(MemoryItem.Scope.USER if explicit_global else MemoryItem.Scope.USER_CONVERSATION),
            is_explicit=explicit_global,
        )]
    return []


def validate_candidate(candidate: MemoryCandidate) -> bool:
    return (
        bool(candidate.content.strip())
        and candidate.category in MemoryItem.Category.values
        and candidate.importance in MemoryItem.Importance.values
        and candidate.retention_level in MemoryItem.Retention.values
        and candidate.visibility in MemoryItem.Visibility.values
        and 0 <= candidate.confidence <= 1
    )
