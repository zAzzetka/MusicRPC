"""Сравнение названий: нужно для поиска обложек и для «слушать вместе»."""
from __future__ import annotations

import re
from difflib import SequenceMatcher


def norm(text: str) -> str:
    """Нижний регистр, ё=е, без знаков препинания и лишних пробелов."""
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_ok(ours: str, theirs: str) -> bool:
    """Одно и то же название? Допускаются пометки вроде «(Remastered)» и мелкие расхождения."""
    a, b = norm(ours), norm(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def artist_ok(ours: str, candidates) -> bool:
    """Исполнитель совпал хотя бы с одним из кандидатов (по вхождению)."""
    a = norm(ours)
    if not a:
        return True  # исполнитель неизвестен — не придираемся
    if isinstance(candidates, str):
        candidates = [candidates]
    for c in candidates:
        n = norm(c)
        if n and (n in a or a in n):
            return True
    return False


def same_track(title1: str, artist1: str, title2: str, artist2: str) -> bool:
    """Это один и тот же трек? Нужны совпадение названия и (если известны) исполнителей."""
    if not title_ok(title1, title2):
        return False
    if not norm(artist1) or not norm(artist2):
        return True
    return artist_ok(artist1, [artist2])
