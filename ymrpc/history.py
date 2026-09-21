"""Недавние треки: хранятся только в памяти и пропадают при выходе."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .textmatch import norm


@dataclass
class HistoryItem:
    when: float
    artist: str
    title: str
    link: str = ""

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


class History:
    def __init__(self, maxlen: int = 10):
        self._items = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    @staticmethod
    def _same(item: HistoryItem, artist: str, title: str) -> bool:
        return norm(item.artist) == norm(artist) and norm(item.title) == norm(title)

    def add(self, artist: str, title: str, link: str = "") -> None:
        """Записывает трек; подряд идущие повторы не дублируются."""
        with self._lock:
            if self._items and self._same(self._items[0], artist, title):
                if link and not self._items[0].link:
                    self._items[0].link = link
                return
            self._items.appendleft(HistoryItem(time.time(), artist, title, link))

    def set_link(self, artist: str, title: str, link: str) -> None:
        if not link:
            return
        with self._lock:
            for item in self._items:
                if self._same(item, artist, title) and not item.link:
                    item.link = link
                    return

    def items(self) -> list:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
