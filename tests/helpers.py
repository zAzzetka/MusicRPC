"""Общие подставные объекты для тестов: не нужны ни Windows, ни Discord, ни интернет."""
from __future__ import annotations

import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Поддельный winrt (медиа-центр Windows)
# ---------------------------------------------------------------------------
class FakeProps:
    def __init__(self, title, artist, album):
        self.title, self.artist, self.album_title = title, artist, album


class FakeControls:
    def __init__(self, play=True, pause=True, position=True):
        self.is_play_enabled, self.is_pause_enabled, self.is_playback_position_enabled = play, pause, position


class FakeInfo:
    def __init__(self, status, controls=None):
        self.playback_status = status
        self.controls = controls or FakeControls()


class FakeTimeline:
    def __init__(self, pos, end):
        self.start_time = timedelta(0)
        self.end_time = timedelta(seconds=end)
        self.position = timedelta(seconds=pos)
        self.last_updated_time = datetime.now(timezone.utc)


class FakeSession:
    def __init__(self, app, title, artist, album, status, pos=5, end=180, controls=None):
        self.source_app_user_model_id = app
        self._props = FakeProps(title, artist, album)
        self._info = FakeInfo(status, controls)
        self._tl = FakeTimeline(pos, end)
        self.commands = []

    async def try_get_media_properties_async(self):
        return self._props

    def get_playback_info(self):
        return self._info

    def get_timeline_properties(self):
        return self._tl

    async def try_play_async(self):
        self.commands.append(("play",))
        self._info.playback_status = 4
        return True

    async def try_pause_async(self):
        self.commands.append(("pause",))
        self._info.playback_status = 5
        return True

    async def try_change_playback_position_async(self, ticks):
        self.commands.append(("seek", ticks))
        return True


class FakeWinrt:
    """Подменяет пакет winrt в sys.modules. sessions — общий список, его можно менять по ходу теста."""

    def __init__(self):
        self.sessions = []
        self.current = None
        outer = self

        class Manager:
            def get_sessions(self):
                return list(outer.sessions)

            def get_current_session(self):
                return outer.current

        class ManagerCls:
            @staticmethod
            async def request_async():
                return Manager()

        self._mod = types.ModuleType("winrt.windows.media.control")
        self._mod.GlobalSystemMediaTransportControlsSessionManager = ManagerCls

    def install(self):
        self._saved = {n: sys.modules.get(n) for n in
                       ("winrt", "winrt.windows", "winrt.windows.media", "winrt.windows.media.control")}
        for name in ("winrt", "winrt.windows", "winrt.windows.media"):
            sys.modules[name] = types.ModuleType(name)
        sys.modules["winrt.windows.media.control"] = self._mod
        return self

    def uninstall(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Поддельный Discord
# ---------------------------------------------------------------------------
class FakeRPC:
    instances = []

    def __init__(self, cid, fail=None, pipe=0):
        self.cid, self.pipe, self.calls = cid, pipe, []
        self.fail_connect, self.fail_next, self.sock_writer = fail, None, None
        FakeRPC.instances.append(self)

    async def connect(self):
        if self.fail_connect:
            raise self.fail_connect

    async def update(self, **kw):
        if self.fail_next:
            err, self.fail_next = self.fail_next, None
            raise err
        self.calls.append(("update", kw))

    async def clear(self):
        self.calls.append(("clear", {}))


def rpc_factory(cid, pipe=0):
    return FakeRPC(cid, pipe=pipe)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# «Брокер в памяти» для проверки Together без сети
# ---------------------------------------------------------------------------
class MemoryBus:
    """Ведёт себя как MQTT-брокер: подписки, retained-сообщения и «завещание» (will)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.subs = {}       # topic -> [MemoryLink]
        self.retained = {}   # topic -> payload

    def link_factory(self):
        def make(brokers, subscribe_topic, will_topic, on_message, on_status):
            return MemoryLink(self, subscribe_topic, will_topic, on_message, on_status)
        return make

    def deliver(self, topic, payload, retain, origin=None):
        with self.lock:
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [lk for lk in self.subs.get(topic, []) if lk is not origin]
        for lk in targets:
            lk.on_message(topic, payload, False)


class MemoryLink:
    def __init__(self, bus, subscribe_topic, will_topic, on_message, on_status):
        self.bus, self.topic, self.will = bus, subscribe_topic, will_topic
        self.on_message, self.on_status = on_message, on_status
        self.up = False
        self.published = []

    def start(self):
        with self.bus.lock:
            self.bus.subs.setdefault(self.topic, []).append(self)
            retained = self.bus.retained.get(self.topic)
        self.up = True
        self.on_status("online", "memory")
        if retained:
            self.on_message(self.topic, retained, True)

    def stop(self):
        self.up = False
        with self.bus.lock:
            if self in self.bus.subs.get(self.topic, []):
                self.bus.subs[self.topic].remove(self)

    def crash(self):
        """Обрыв без предупреждения: брокер публикует «завещание»."""
        self.up = False
        with self.bus.lock:
            if self in self.bus.subs.get(self.topic, []):
                self.bus.subs[self.topic].remove(self)
        if self.will:
            self.bus.deliver(self.will, b"", True)

    def publish(self, topic, payload, retain=False):
        if not self.up:
            return False
        self.published.append((topic, payload, retain))
        self.bus.deliver(topic, payload, retain, origin=self)
        return True
