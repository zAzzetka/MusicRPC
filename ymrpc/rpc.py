"""Сборка статуса для Discord и фоновый рабочий цикл."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field

from pypresence import (
    ActivityType,
    DiscordError,
    DiscordNotFound,
    InvalidID,
    PyPresenceException,
    ServerError,
)

try:  # появилось в pypresence 4.5+
    from pypresence import StatusDisplayType
except ImportError:  # pragma: no cover
    StatusDisplayType = None

from .config import ConfigStore
from .covers import CoverResolver, Resolved
from .history import History
from .media import MediaReader, Track
from .status import Status
from .together import SyncState, Together, open_url, plan_sync

log = logging.getLogger("ymrpc.rpc")

DEFAULT_BUTTON_LABEL = "Открыть трек"
CONNECT_TIMEOUT = 8.0  # сколько ждать ответа Discord на рукопожатие (сразу после старта Windows он «молчит»)
MIN_GAP = 2.0          # не чаще одного обновления в N секунд (у Discord есть лимит)
DRIFT = 4              # на сколько секунд должна «уехать» позиция, чтобы пересинхронизироваться (перемотка)
DRIFT_GAP = 15.0       # ...но не чаще, чем раз в N секунд (лимит Discord — примерно одно обновление в 15 с)
NONE_GRACE = 4.0       # при смене трека плеер на секунду «теряет» название — статус не убираем сразу
REJECT_BACKOFF = 15.0  # если Discord отклонил статус целиком, повторяем не чаще, чем раз в N секунд
COVER_WAIT = 4.0       # сколько максимум ждать обложку перед первым показом трека
RECONNECT_GAP = 10.0   # как часто пытаться подключиться к Discord
MAX_PIPES = 10         # у Discord каналы discord-ipc-0 … discord-ipc-9


# ---------------------------------------------------------------------------
# Чистая логика: как из трека и настроек получить статус
# ---------------------------------------------------------------------------
class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, values: dict) -> str:
    """Подставляет {title} {artist} {album} в шаблон."""
    try:
        text = template.format_map(_Safe(values))
    except Exception:
        text = template
    text = re.sub(r"\s+", " ", text).strip()
    if any(not v for v in values.values()):
        text = text.strip(" -–—|·•")  # убираем «висящие» разделители, если чего-то не хватило
    return text


def fit(text: str, limit: int = 120) -> str:
    """Discord требует от 2 до 128 символов."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    if len(text) < 2:
        text += "\u200b"
    return text


def is_hidden(track: Track, keywords) -> bool:
    """Попадает ли трек под фильтр «не показывать» (слово в названии, исполнителе, альбоме или плеере)."""
    words = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
    if not words:
        return False
    haystack = " ".join((track.title, track.artist, track.album, track.app_id)).lower()
    return any(w in haystack for w in words)


@dataclass
class Activity:
    static: dict = field(default_factory=dict)  # всё, кроме времени
    start: int | None = None
    end: int | None = None

    def signature(self) -> tuple:
        return (json.dumps(self.static, sort_keys=True, default=int), self.start is not None, self.end is not None)


def build_activity(track: Track, cfg, resolved: Resolved, now: float, fallback_start: float | None = None,
                   together_name: str | None = None) -> Activity:
    values = {"title": track.title, "artist": track.artist, "album": track.album}
    details = fit(render(cfg.details_format, values))
    state = fit(render(cfg.state_format, values))
    large_text = fit(render(cfg.large_text_format, values))

    if together_name and cfg.together_suffix:  # «… · вместе с Аней»
        suffix = cfg.together_suffix.replace("{name}", together_name)
        state = fit(f"{state} {suffix}" if state else suffix)
    if not track.playing:
        state = fit(f"⏸ {state}" if state else "⏸ Пауза")

    static: dict = {"activity_type": ActivityType.LISTENING}
    if details:
        static["details"] = details
    if state:
        static["state"] = state
    if cfg.show_cover and resolved.cover_url:
        static["large_image"] = resolved.cover_url
        if large_text:
            static["large_text"] = large_text
    if cfg.show_button and resolved.track_url:
        static["buttons"] = [{"label": cfg.button_label or DEFAULT_BUTTON_LABEL, "url": resolved.track_url}]
    if StatusDisplayType is not None and cfg.status_display in ("details", "state"):
        static["status_display_type"] = (
            StatusDisplayType.DETAILS if cfg.status_display == "details" else StatusDisplayType.STATE
        )

    start = end = None
    if track.playing and cfg.show_progress:
        if track.position <= 0 and track.duration <= 0 and fallback_start is not None:
            start = int(fallback_start)  # плеер не сообщает позицию — считаем сами
        else:
            start = int(now - track.position)
            if track.duration > 0:
                end = int(start + track.duration)
    return Activity(static, start, end)


def _hard_close(rpc) -> None:
    """Закрывает канал до Discord. rpc.close() из pypresence не подходит: он закрывает event loop."""
    try:
        writer = getattr(rpc, "sock_writer", None)
        if writer is not None:
            writer.close()
    except Exception:
        pass


def _default_rpc_factory(client_id: str, pipe: int | None = None):
    from pypresence import AioPresence

    return AioPresence(client_id, pipe=pipe, loop=asyncio.get_running_loop())


async def open_presence(factory, client_id: str, timeout: float = CONNECT_TIMEOUT):
    """Подключается к первому живому каналу Discord.

    Перебирает каналы 0…9 и не ждёт ответа дольше `timeout` секунд: только что запущенный Discord
    (например, сразу после входа в Windows) создаёт канал раньше, чем начинает отвечать,
    а библиотека в таком случае ждёт бесконечно.
    Возвращает (rpc | None, [(номер канала, ошибка), …]). Неверный Client ID — исключение InvalidID.
    """
    errors = []
    for pipe in range(MAX_PIPES):
        rpc = None
        try:
            rpc = factory(client_id, pipe)
            await asyncio.wait_for(rpc.connect(), timeout)
            return rpc, errors
        except InvalidID:
            if rpc is not None:
                _hard_close(rpc)
            raise
        except Exception as e:
            errors.append((pipe, e))
            if rpc is not None:
                _hard_close(rpc)
    return None, errors


async def run_blocking(func, *args):
    """Выполняет блокирующую функцию в потоке. (asyncio.to_thread есть только с Python 3.9.)"""
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


def describe_error(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


# ---------------------------------------------------------------------------
# Рабочий цикл
# ---------------------------------------------------------------------------
class Worker:
    def __init__(
        self,
        store: ConfigStore,
        status: Status,
        reader=None,
        resolver=None,
        rpc_factory=None,
        clock=time.time,
        together=None,
        history=None,
    ):
        self.store = store
        self.status = status
        self.reader = reader or MediaReader()
        self.resolver = resolver or CoverResolver(lambda: self.store.get().yandex_token)
        self.together = together if together is not None else Together(on_change=self.wake)
        self.history = history if history is not None else History()
        self._rpc_factory = rpc_factory or _default_rpc_factory
        self._clock = clock

        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None

        self._rpc = None
        self._client_id = ""
        self._conn_error = None
        self._last_conn_msg = None
        self._tried_id = None
        self._next_connect = 0.0

        self._active = False
        self._last_sig = None
        self._last_start = None
        self._last_sent = 0.0
        self._reject_sig = None
        self._retry_after = 0.0
        self._pending: dict = {}

        self._timer_key = None       # собственный таймер — для плееров, которые не сообщают позицию
        self._timer_elapsed = 0.0
        self._timer_last = 0.0
        self._none_since = None      # с какого момента трека нет
        self._read_error = None
        self._sync = SyncState()
        self._current = (None, None, None)   # (что показано, ссылка на трек, имя хоста)

    # -- управление из других потоков --------------------------------------
    def run_forever(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("Рабочий поток остановился из-за ошибки")

    def stop(self) -> None:
        self._stop.set()
        self.wake()

    def wake(self) -> None:
        """Применить изменения настроек немедленно."""
        loop, ev = self._loop, self._wake
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass

    def list_sources(self) -> concurrent.futures.Future:
        """Что сейчас видит Windows (для кнопки «Проверить источники»)."""
        if self._loop is None:
            fut: concurrent.futures.Future = concurrent.futures.Future()
            fut.set_exception(RuntimeError("Рабочий поток ещё не запущен"))
            return fut
        return asyncio.run_coroutine_threadsafe(self.reader.list_sources(), self._loop)

    def current(self) -> tuple:
        """(показанный трек | None, ссылка на него | None, имя хоста | None) — для меню в трее."""
        return self._current

    # -- основной цикл ------------------------------------------------------
    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        log.info("Рабочий поток запущен")
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка в рабочем цикле")
                self.status.set("error")
            await self._sleep(self.store.get().poll_interval)
        await self._shutdown()
        log.info("Рабочий поток остановлен")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _shutdown(self) -> None:
        await self._clear()
        if self._rpc is not None:
            _hard_close(self._rpc)
            self._rpc = None
        await run_blocking(self.together.stop)

    # -- один шаг цикла -----------------------------------------------------
    async def _read_local(self, cfg):
        """Что играет у нас: (трек | None, скрыт ли фильтром, ошибка чтения)."""
        try:
            track = await self.reader.read(cfg.app_filters)
            self._read_error = None
        except Exception as e:
            message = describe_error(e)
            if message != self._read_error:  # в лог — один раз, а не каждые 2 секунды
                log.warning("Не удалось прочитать медиа-центр Windows: %s", message)
            self._read_error = message
            return None, False, True
        if track is not None and is_hidden(track, cfg.hide_keywords):
            return None, True, False
        return track, False, False

    def _own_timer(self, track: Track, now: float) -> float:
        """Время начала трека по собственным часам (учитывает паузы). Нужно, если плеер не сообщает позицию."""
        if track.key != self._timer_key:
            self._timer_key, self._timer_elapsed = track.key, 0.0
        elif track.playing:
            self._timer_elapsed += max(now - self._timer_last, 0.0)
        self._timer_last = now
        return now - self._timer_elapsed

    async def _together_step(self, cfg, local, remote, now) -> None:
        """Хост публикует, что играет; гость подстраивает свой плеер под хоста."""
        if cfg.together_mode == "host":
            resolved = await self._resolve(local) if local is not None else Resolved()
            self.together.publish_local(local, resolved.cover_url, resolved.track_url, now)
        elif cfg.together_mode == "guest" and remote is not None:
            actions = plan_sync(local, remote, now, self._sync,
                                sync_player=cfg.together_sync_player, auto_open=cfg.together_auto_open)
            for action in actions:
                await self._do_sync(cfg, remote, action, now)

    async def _do_sync(self, cfg, remote, action: tuple, now: float) -> None:
        kind = action[0]
        if kind == "open":
            self._sync.opened_key = remote.key
            await run_blocking(open_url, action[1])
            return
        ok = await self.reader.send_command(cfg.app_filters, kind, action[1] if len(action) > 1 else None)
        self._sync.last_cmd = now
        log.info("Синхронизация: %s%s → %s", kind, f" {action[1]:.0f} с" if len(action) > 1 else "",
                 "выполнено" if ok else "плеер отказал")
        if kind == "seek":
            self._sync.last_seek = now
            if ok:
                self._sync.seek_failures = 0
            else:
                self._sync.seek_failures += 1
                if self._sync.seek_failures >= 2:  # плеер не умеет перематывать — не мучаем его
                    self._sync.seek_ok = False

    async def _tick(self) -> None:
        cfg = self.store.get()
        now = self._clock()
        self.together.configure(cfg)
        self.together.tick(now)

        local, hidden, read_failed = await self._read_local(cfg)
        remote = self.together.remote(now) if cfg.together_mode == "guest" else None
        await self._together_step(cfg, local, remote, now)

        # --- что показывать в Discord: свой трек или трек хоста ---
        shown, resolved, label = local, None, None
        if remote is not None and cfg.together_mirror:
            remote_track = remote.as_track(now)
            if is_hidden(remote_track, cfg.hide_keywords):
                shown, hidden = None, True
            else:
                shown, label = remote_track, remote.host_name
                resolved = Resolved(remote.cover or None, remote.link or None, "together")
        self._current = (shown, resolved.track_url if resolved else None, label)

        if not cfg.enabled:
            await self._clear()
            self.status.set("disabled")
            return
        if not cfg.client_id:
            await self._clear()
            self.status.set("no_client_id")
            return

        if shown is None:
            if hidden:
                await self._clear()
                self.status.set("hidden")
                return
            # трека нет: не убираем статус сразу — при смене трека плеер на секунду теряет название
            if self._active and not read_failed:
                self._none_since = self._none_since if self._none_since is not None else now
                if now - self._none_since < NONE_GRACE:
                    return
            await self._clear()
            self.status.set("error" if read_failed else "waiting")
            return
        self._none_since = None

        fallback_start = self._own_timer(shown, now)
        if not shown.playing and not cfg.show_paused:
            await self._clear()
            self.status.set("paused", shown)
            return

        problem = await self._connect(cfg.client_id)
        if problem:
            self.status.set(problem, shown)
            return

        if cfg.show_cover or cfg.show_button:
            if resolved is None or not resolved.cover_url or not resolved.track_url:
                found = await self._resolve(shown)   # чего не хватает (например, обложки) — ищем сами
                resolved = Resolved(
                    (resolved.cover_url if resolved else None) or found.cover_url,
                    (resolved.track_url if resolved else None) or found.track_url,
                    found.source,
                )
        else:
            resolved = Resolved(None, resolved.track_url if resolved else None)
        self._current = (shown, resolved.track_url, label)
        self.history.add(shown.artist, shown.title, resolved.track_url or "")

        activity = build_activity(shown, cfg, resolved, now, fallback_start=fallback_start, together_name=label)
        ok = await self._push(activity, now)
        if ok:
            self.status.set("playing" if shown.playing else "paused", shown)
        else:
            self.status.set("discord_missing", shown)

    # -- Discord ------------------------------------------------------------
    async def _connect(self, client_id: str) -> str | None:
        """None — подключены; иначе код проблемы для статуса."""
        if self._rpc is not None and self._client_id == client_id:
            return None
        if self._rpc is not None:  # Client ID сменили в настройках
            await self._drop_connection()
            self._next_connect = 0.0

        now = self._clock()
        if client_id != self._tried_id:  # ID поменяли — пробуем сразу, а не через паузу
            self._tried_id, self._next_connect = client_id, 0.0
        if now < self._next_connect:
            return self._conn_error

        try:
            rpc, errors = await open_presence(self._rpc_factory, client_id)
        except InvalidID:
            self._report_connect("bad_client_id", f"Discord отклонил Client ID {client_id!r}", logging.ERROR)
            self._next_connect = now + 30
            return self._conn_error
        except Exception as e:
            self._report_connect("discord_error", f"Ошибка подключения к Discord: {describe_error(e)}", logging.WARNING)
            self._next_connect = now + RECONNECT_GAP
            return self._conn_error

        if rpc is not None:
            log.info("Подключились к Discord")
            self._rpc, self._client_id, self._conn_error, self._last_conn_msg = rpc, client_id, None, None
            return None

        # ни один канал не подошёл
        found = [(p, e) for p, e in errors if not isinstance(e, DiscordNotFound)]
        if found:  # канал есть, но подключиться не вышло — Discord только запускается или занят
            details = "; ".join(f"канал {p}: {describe_error(e)}" for p, e in found)
            self._report_connect("discord_error", f"Discord найден, но не отвечает ({details})", logging.WARNING)
        else:
            self._report_connect("discord_missing", "Discord не найден (не запущен или запущен не как приложение)", logging.INFO)
        self._next_connect = now + RECONNECT_GAP
        return self._conn_error

    def _report_connect(self, code: str, message: str, level: int) -> None:
        """Запоминает причину и пишет её в лог один раз, а не каждые 10 секунд."""
        self._conn_error = code
        if message != self._last_conn_msg:
            self._last_conn_msg = message
            log.log(level, message)

    async def _drop_connection(self) -> None:
        if self._rpc is not None:
            _hard_close(self._rpc)
        self._rpc = None
        self._active = False
        self._last_sig = None

    async def _clear(self) -> None:
        """Убрать статус из профиля (если он там был)."""
        if not self._active or self._rpc is None:
            self._active = False
            self._last_sig = None
            return
        try:
            await self._rpc.clear()
            log.info("Статус убран")
        except Exception as e:
            log.info("Не удалось очистить статус: %s", e)
            await self._drop_connection()
        self._active = False
        self._last_sig = None
        self._last_sent = self._clock()

    async def _push(self, act: Activity, now: float) -> bool:
        """Отправляет статус, если он изменился. False — потеряна связь с Discord."""
        sig = act.signature()
        need = (not self._active) or sig != self._last_sig
        if not need and act.start is not None and self._last_start is not None:
            if abs(act.start - self._last_start) >= DRIFT and now - self._last_sent >= DRIFT_GAP:
                need = True  # трек перемотали
        if not need:
            return True
        if now - self._last_sent < MIN_GAP:
            return True  # слишком часто — отправим на следующем тике
        if sig == self._reject_sig and now < self._retry_after:
            return True  # этот статус Discord уже отклонил — подождём, а не будем долбить

        kwargs = dict(act.static)
        if act.start is not None:
            kwargs["start"] = act.start
        if act.end is not None:
            kwargs["end"] = act.end

        try:
            await self._rpc.update(**kwargs)
        except (ServerError, DiscordError) as e:
            # Discord не принял статус — пробуем упрощённый (без картинки и кнопок)
            log.warning("Discord отклонил статус (%s), пробую упрощённый", e)
            minimal = {k: v for k, v in kwargs.items() if k in ("activity_type", "details", "state", "start", "end")}
            try:
                await self._rpc.update(**minimal)
            except Exception as e2:
                log.warning("Упрощённый статус тоже отклонён: %s", e2)
                self._last_sent = now
                self._reject_sig, self._retry_after = sig, now + REJECT_BACKOFF
                return True
        except (PyPresenceException, OSError, asyncio.TimeoutError) as e:
            log.info("Связь с Discord потеряна: %s", e)
            await self._drop_connection()
            self._next_connect = now + RECONNECT_GAP
            self._conn_error = "discord_missing"
            self._last_conn_msg = None
            return False

        self._active = True
        self._last_sig = sig
        self._last_start = act.start
        self._last_sent = now
        log.info("Статус обновлён: %s — %s", kwargs.get("state", ""), kwargs.get("details", ""))
        return True

    # -- обложки ------------------------------------------------------------
    async def _resolve(self, track: Track) -> Resolved:
        cached = self.resolver.peek(track.artist, track.title)
        if cached is not None:
            return cached
        key = (track.artist, track.title)
        task = self._pending.get(key)
        if task is None:
            task = asyncio.ensure_future(
                run_blocking(self.resolver.resolve, track.artist, track.title, track.album)
            )
            self._pending[key] = task
            task.add_done_callback(lambda _t, k=key: self._pending.pop(k, None))
        try:
            found = await asyncio.wait_for(asyncio.shield(task), COVER_WAIT)
            self.history.set_link(track.artist, track.title, found.track_url or "")
            return found
        except Exception:
            # не успели/не вышло — покажем без обложки, она подтянется на одном из следующих тиков
            return Resolved()
