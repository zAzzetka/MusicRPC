"""Поиск обложки и ссылки на трек.

Discord умеет показывать картинку только по ссылке, а Windows отдаёт обложку
лишь как поток данных. Поэтому обложку ищем по названию и исполнителю:
сначала в поиске Яндекс Музыки, затем (если не нашлось) в Deezer и iTunes.
Всё это публичные запросы без входа в аккаунт.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .textmatch import artist_ok as _artist_ok
from .textmatch import norm as _norm
from .textmatch import title_ok as _title_ok

log = logging.getLogger("ymrpc.covers")

TIMEOUT = 6
NEGATIVE_TTL = 120     # через сколько секунд повторять неудачный поиск
MAX_CACHE = 300


@dataclass
class Resolved:
    cover_url: str | None = None
    track_url: str | None = None
    source: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.cover_url or self.track_url)


# --- HTTP (только стандартная библиотека: никаких сторонних пакетов, которые могут не поддерживать новый Python) ---
def http_get_json(url: str, params: dict | None = None, headers: dict | None = None):
    """GET-запрос, ответ разбирается как JSON. Любая ошибка сети или статуса — исключение."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "MusicRPC/1.2", **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read(2_000_000).decode("utf-8", "replace"))


# --- источники ------------------------------------------------------------
def _from_yandex(fetch, artist: str, title: str, token: str) -> Resolved | None:
    headers = {"X-Yandex-Music-Client": "YandexMusicAndroid/24023231"}
    if token:
        headers["Authorization"] = f"OAuth {token}"
    data = fetch(
        "https://api.music.yandex.net/search",
        {"text": f"{artist} {title}".strip(), "type": "track", "page": 0, "nocorrect": "false"},
        headers,
    )
    results = (((data.get("result") or {}).get("tracks") or {}).get("results")) or []
    for item in results[:6]:
        names = [a.get("name", "") for a in item.get("artists", [])]
        full_title = item.get("title", "")
        version = item.get("version")
        if not (_title_ok(title, full_title) or (version and _title_ok(title, f"{full_title} {version}"))):
            continue
        if not _artist_ok(artist, names):
            continue
        albums = item.get("albums") or []
        album_id = albums[0].get("id") if albums else None
        track_id = str(item.get("realId") or item.get("id") or "").split(":")[0]
        cover = item.get("coverUri") or (albums[0].get("coverUri") if albums else None)
        cover_url = "https://" + cover.replace("%%", "400x400") if cover else None
        track_url = None
        if track_id and album_id:
            track_url = f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
        elif track_id:
            track_url = f"https://music.yandex.ru/track/{track_id}"
        return Resolved(cover_url, track_url, "yandex")
    return None


def _from_deezer(fetch, artist: str, title: str, token: str) -> Resolved | None:
    data = fetch("https://api.deezer.com/search", {"q": f"{artist} {title}".strip(), "limit": 6}, None)
    for item in data.get("data", []):
        if not _title_ok(title, item.get("title", "")):
            continue
        if not _artist_ok(artist, [(item.get("artist") or {}).get("name", "")]):
            continue
        album = item.get("album") or {}
        cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
        if cover:
            return Resolved(cover, None, "deezer")
    return None


def _from_itunes(fetch, artist: str, title: str, token: str) -> Resolved | None:
    data = fetch("https://itunes.apple.com/search",
                 {"term": f"{artist} {title}".strip(), "entity": "song", "limit": 6}, None)
    for item in data.get("results", []):
        if not _title_ok(title, item.get("trackName", "")):
            continue
        if not _artist_ok(artist, [item.get("artistName", "")]):
            continue
        art = item.get("artworkUrl100")
        if art:
            return Resolved(re.sub(r"\d+x\d+bb", "600x600bb", art), None, "itunes")
    return None


class CoverResolver:
    SOURCES = (_from_yandex, _from_deezer, _from_itunes)

    def __init__(self, token_getter=lambda: "", fetch=None):
        self._token_getter = token_getter
        self._fetch = fetch or http_get_json
        self._cache: dict[tuple, tuple[float, Resolved]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(artist: str, title: str) -> tuple:
        return (_norm(artist), _norm(title))

    def peek(self, artist: str, title: str) -> Resolved | None:
        """Готовый результат из кэша или None, если его ещё нет."""
        with self._lock:
            hit = self._cache.get(self._key(artist, title))
        if hit is None:
            return None
        stamp, res = hit
        if not res.found and time.time() - stamp > NEGATIVE_TTL:
            return None
        return res

    def resolve(self, artist: str, title: str, album: str = "") -> Resolved:
        """Блокирующий поиск (вызывать из отдельного потока). Никогда не бросает исключений."""
        cached = self.peek(artist, title)
        if cached is not None:
            return cached
        token = ""
        try:
            token = self._token_getter() or ""
        except Exception:
            pass

        result = Resolved()
        for source in self.SOURCES:
            name = source.__name__[len("_from_"):]
            try:
                found = source(self._fetch, artist, title, token)
            except Exception as e:
                log.info("Поиск обложки (%s) не удался: %s", name, e)
                continue
            if found is None:
                continue
            # берём лучшее из всех источников: обложку и ссылку — где они есть
            result.cover_url = result.cover_url or found.cover_url
            result.track_url = result.track_url or found.track_url
            result.source = result.source or found.source
            if result.cover_url and result.track_url:
                break

        with self._lock:
            if len(self._cache) >= MAX_CACHE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[self._key(artist, title)] = (time.time(), result)
        log.info("Обложка для «%s — %s»: %s", artist, title, result.source or "не найдена")
        return result
