"""Чтение «что сейчас играет» из системного медиа-центра Windows (SMTC).

Плеер (Яндекс Музыка, Spotify, браузер — любой) сообщает Windows название трека,
исполнителя и позицию — ровно то, что показывается в шторке громкости.
Никаких входов в аккаунты для этого не нужно.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger("ymrpc.media")

_PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


@dataclass
class Track:
    app_id: str        # идентификатор приложения-источника
    title: str
    artist: str
    album: str
    playing: bool
    position: float    # секунд от начала трека (уже с поправкой на прошедшее время)
    duration: float    # секунд; 0 — если плеер не сообщил длительность
    is_current: bool = False   # Windows считает этот плеер «текущим» (тем, что управляется медиа-клавишами)

    @property
    def key(self) -> tuple:
        return (self.app_id, self.title, self.artist, self.album)


def matches(app_id: str, filters) -> bool:
    """Подходит ли источник под фильтры (пустые фильтры = подходит любой)."""
    parts = [f.strip().lower() for f in (filters or []) if f and f.strip()]
    if not parts:
        return True
    low = (app_id or "").lower()
    return any(p in low for p in parts)


def _to_seconds(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_timeline(session, playing: bool, now: datetime | None = None) -> tuple[float, float]:
    """Возвращает (позиция, длительность) в секундах."""
    try:
        tl = session.get_timeline_properties()
        start = _to_seconds(tl.start_time)
        end = _to_seconds(tl.end_time)
        pos = _to_seconds(tl.position)
        updated = tl.last_updated_time
    except Exception as e:
        log.debug("Нет данных о позиции: %s", e)
        return 0.0, 0.0

    duration = end - start
    if duration < 1:
        duration = 0.0
    pos = max(pos - start, 0.0)

    # Позиция в SMTC фиксируется в момент last_updated_time — добавляем прошедшее с тех пор
    if playing and isinstance(updated, datetime) and updated.year > 2000:
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        elapsed = (now - updated).total_seconds()
        if 0 < elapsed < 3 * 3600:
            pos += elapsed

    if duration:
        pos = min(pos, duration)
    return pos, duration


def _pick(pairs):
    """Из подходящих (трек, сессия) выбирает лучшую: играющая, и предпочтительно «текущая» для Windows."""
    playing = [p for p in pairs if p[0].playing]
    if not playing:
        return pairs[0] if pairs else None
    for track, session in playing:
        if track.is_current:
            return track, session
    return playing[0]


class MediaReader:
    def __init__(self):
        self._manager = None

    async def _get_manager(self):
        if self._manager is None:
            try:
                from winrt.windows.media.control import (
                    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
                )
            except ImportError as e:
                raise RuntimeError(
                    "не установлены пакеты winrt-* (запустите install.bat) или Python слишком новый для них: "
                    f"{e}") from e
            self._manager = await SessionManager.request_async()
        return self._manager

    async def _read_session(self, session, include_empty: bool) -> Track | None:
        try:
            app_id = session.source_app_user_model_id or ""
            props = await session.try_get_media_properties_async()
            info = session.get_playback_info()
            playing = int(info.playback_status) == _PLAYING
            title = (props.title if props else "") or ""
            artist = (props.artist if props else "") or ""
            album = (props.album_title if props else "") or ""
            if not title.strip() and not include_empty:
                return None
            position, duration = read_timeline(session, playing)
        except Exception as e:
            log.debug("Не удалось прочитать сессию: %s", e)
            return None
        return Track(app_id, title.strip(), artist.strip(), album.strip(), playing, position, duration)

    async def _pairs(self, include_empty: bool = False) -> list:
        """Все сессии в виде [(Track, сессия)]."""
        try:
            manager = await self._get_manager()
            sessions = list(manager.get_sessions())
            try:
                current = manager.get_current_session()
                current_id = (current.source_app_user_model_id or "") if current is not None else None
            except Exception:
                current_id = None
        except Exception:
            self._manager = None  # в следующий раз получим менеджер заново
            raise
        pairs = []
        for session in sessions:
            track = await self._read_session(session, include_empty)
            if track is not None:
                track.is_current = current_id is not None and track.app_id == current_id
                pairs.append((track, session))
        return pairs

    async def read(self, filters) -> Track | None:
        """Лучший подходящий трек: сначала играющий, потом на паузе."""
        pairs = [p for p in await self._pairs() if matches(p[0].app_id, filters)]
        pick = _pick(pairs)
        return pick[0] if pick else None

    async def list_sources(self) -> list:
        """Все источники, которые видит Windows (для диагностики)."""
        return [t for t, _ in await self._pairs(include_empty=True)]

    async def send_command(self, filters, action: str, value: float | None = None) -> bool:
        """Управляет тем же плеером, что и read(): action = play | pause | seek (value — секунды).

        Возвращает True, если плеер принял команду. Не все плееры разрешают перемотку.
        """
        pairs = [p for p in await self._pairs() if matches(p[0].app_id, filters)]
        pick = _pick(pairs)
        if pick is None:
            return False
        session = pick[1]
        try:
            controls = session.get_playback_info().controls
            if action == "play":
                return bool(controls.is_play_enabled and await session.try_play_async())
            if action == "pause":
                return bool(controls.is_pause_enabled and await session.try_pause_async())
            if action == "seek":
                ticks = int(max(float(value or 0.0), 0.0) * 10_000_000)  # SMTC считает в 100-наносекундных «тиках»
                return bool(controls.is_playback_position_enabled
                            and await session.try_change_playback_position_async(ticks))
        except Exception as e:
            log.debug("Команда %s не выполнена: %s", action, e)
        return False
