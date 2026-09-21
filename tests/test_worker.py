import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pypresence import DiscordNotFound, InvalidID, PipeClosed, ServerError

import ymrpc.rpc as rpcmod
from tests.helpers import Clock, FakeRPC, FakeSession, FakeWinrt, MemoryBus, rpc_factory
from ymrpc.config import ConfigStore
from ymrpc.covers import Resolved
from ymrpc.media import MediaReader
from ymrpc.rpc import Worker
from ymrpc.status import Status
from ymrpc.together import Together, generate_code

PLAYING, PAUSED = 4, 5
COVER = Resolved("https://c/x.jpg", "https://music.yandex.ru/track/1", "yandex")


class FakeResolver:
    def __init__(self):
        self.cache = {}

    def peek(self, artist, title):
        return self.cache.get((artist, title))

    def resolve(self, artist, title, album):
        self.cache[(artist, title)] = COVER
        return COVER


class WorkerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.win = FakeWinrt().install()
        self.addCleanup(self.win.uninstall)
        FakeRPC.instances.clear()
        self.clock = Clock()
        self.bus = MemoryBus()
        self.store = ConfigStore(Path(tempfile.mkdtemp()) / "c.json")
        self.store.update(client_id="123456789012345678")

    def make(self, factory=rpc_factory, together=None):
        together = together or Together(link_factory=self.bus.link_factory(), clock=self.clock)
        w = Worker(self.store, Status(), reader=MediaReader(), resolver=FakeResolver(),
                   rpc_factory=factory, clock=self.clock, together=together)
        w._loop, w._wake = asyncio.get_running_loop(), asyncio.Event()
        self.addCleanup(lambda: together.stop())
        return w

    def play(self, title="Song", artist="Artist", status=PLAYING, pos=10, app="ru.yandex.desktop.music", end=180):
        s = FakeSession(app, title, artist, "Album", status, pos=pos, end=end)
        self.win.sessions[:] = [s]
        return s

    @property
    def rpc(self):
        return FakeRPC.instances[0]


class PresenceTests(WorkerCase):
    async def test_lifecycle(self):
        w = self.make()
        self.play(pos=10)
        await w._tick()
        kw = self.rpc.calls[0][1]
        self.assertEqual((kw["details"], kw["state"]), ("Song", "Artist"))
        self.assertTrue(kw["large_image"].startswith("https://c/"))
        self.assertIn("end", kw)
        self.assertEqual(w.status.state, "playing")

        self.clock.t += 2
        await w._tick()
        self.assertEqual(len(self.rpc.calls), 1)                    # ничего не менялось — ничего не шлём

        self.clock.t += 3
        self.play("Other", "B", pos=0)
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][1]["details"], "Other")

        self.clock.t += 16                                          # перемотка (позиция «уехала»)
        self.win.sessions[0]._tl.position = self.win.sessions[0]._tl.position.__class__(seconds=100)
        await w._tick()
        self.assertEqual(len(self.rpc.calls), 3)

        self.clock.t += 3
        self.win.sessions[0]._info.playback_status = PAUSED
        await w._tick()
        self.assertEqual((self.rpc.calls[-1][0], w.status.state), ("clear", "paused"))
        n = len(self.rpc.calls)
        self.clock.t += 2
        await w._tick()
        self.assertEqual(len(self.rpc.calls), n)                    # второй раз не очищаем

        self.clock.t += 3
        self.win.sessions[0]._info.playback_status = PLAYING
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][0], "update")

    async def test_track_switch_does_not_flicker_but_closed_player_clears(self):
        w = self.make()
        s = self.play()
        await w._tick()
        s._props.title = ""                                         # при смене трека плеер на миг теряет название
        self.clock.t += 2
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][0], "update")           # не убираем статус сразу
        s._props.title = "Next"
        self.clock.t += 2
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][1]["details"], "Next")
        self.win.sessions[:] = []                                   # плеер закрыли
        self.clock.t += 2
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][0], "update")
        self.clock.t += rpcmod.NONE_GRACE + 1
        await w._tick()
        self.assertEqual((self.rpc.calls[-1][0], w.status.state), ("clear", "waiting"))

    async def test_show_paused_and_disable(self):
        w = self.make()
        self.play(status=PAUSED)
        self.store.update(show_paused=True)
        await w._tick()
        kw = self.rpc.calls[-1][1]
        self.assertTrue(kw["state"].startswith("⏸") and "start" not in kw)
        self.store.update(enabled=False)
        self.clock.t += 3
        await w._tick()
        self.assertEqual((self.rpc.calls[-1][0], w.status.state), ("clear", "disabled"))

    async def test_blocklist(self):
        w = self.make()
        self.play("Guilty Pleasure", "Some Band")
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][0], "update")
        self.store.update(hide_keywords=["guilty"])
        self.clock.t += 3
        await w._tick()
        self.assertEqual((self.rpc.calls[-1][0], w.status.state), ("clear", "hidden"))   # сразу, без задержки
        self.assertIsNone(w.current()[0])

    async def test_own_timer_ignores_paused_time(self):
        w = self.make()
        s = self.play(pos=0, end=0)                                 # плеер не сообщает ни позицию, ни длительность
        s._tl.end_time = s._tl.end_time.__class__(0)
        await w._tick()
        start1 = self.rpc.calls[-1][1]["start"]
        s._info.playback_status = PAUSED
        for _ in range(5):
            self.clock.t += 10
            await w._tick()
        s._info.playback_status = PLAYING
        self.clock.t += 2
        await w._tick()
        start2 = self.rpc.calls[-1][1]["start"]
        self.assertLess(start2 - start1, 70)                        # 50 с паузы не засчитаны как «прослушано»
        self.assertGreater(start2 - start1, 45)

    async def test_no_client_id_and_status_states(self):
        w = self.make()
        self.store.update(client_id="")
        self.play()
        await w._tick()
        self.assertEqual(w.status.state, "no_client_id")
        self.assertFalse(FakeRPC.instances)

    async def test_read_error_is_logged_once(self):
        w = self.make()
        self.win.uninstall()
        import sys
        for n in ("winrt", "winrt.windows", "winrt.windows.media", "winrt.windows.media.control"):
            sys.modules[n] = None
        try:
            with self.assertLogs("ymrpc.rpc", level="WARNING") as cm:
                for _ in range(5):
                    self.clock.t += 2
                    await w._tick()
                    self.assertEqual(w.status.state, "error")
            self.assertEqual(len(cm.records), 1)
        finally:
            for n in ("winrt", "winrt.windows", "winrt.windows.media", "winrt.windows.media.control"):
                sys.modules.pop(n, None)
            self.win.install()


class DiscordConnectionTests(WorkerCase):
    async def test_down_then_up_and_reconnect(self):
        mode = {"fail": DiscordNotFound()}

        def factory(cid, pipe=0):
            r = FakeRPC(cid, pipe=pipe)
            r.fail_connect = mode["fail"]
            return r

        w = self.make(factory)
        self.play()
        attempts = lambda: len([i for i in FakeRPC.instances if i.pipe == 0])  # noqa: E731
        await w._tick()
        self.assertEqual((w.status.state, attempts()), ("discord_missing", 1))
        self.clock.t += 2
        await w._tick()
        self.assertEqual(attempts(), 1)                              # не чаще раза в 10 секунд
        mode["fail"] = None
        self.clock.t += 11
        await w._tick()
        self.assertEqual(w.status.state, "playing")
        FakeRPC.instances[-1].fail_next = PipeClosed()               # связь пропала посреди трека
        self.play("Song2")
        self.clock.t += 3
        await w._tick()
        self.assertEqual((w.status.state, w._rpc), ("discord_missing", None))
        self.clock.t += 11
        await w._tick()
        self.assertEqual((w.status.state, attempts()), ("playing", 3))

    async def test_bad_client_id_then_fixed(self):
        mode = {"fail": InvalidID()}

        def factory(cid, pipe=0):
            r = FakeRPC(cid, pipe=pipe)
            r.fail_connect = mode["fail"]
            return r

        w = self.make(factory)
        self.play()
        await w._tick()
        self.assertEqual(w.status.state, "bad_client_id")
        mode["fail"] = None
        self.store.update(client_id="999999999999999999")
        await w._tick()                                              # без ожидания 30 секунд
        self.assertEqual(w.status.state, "playing")

    async def test_rejected_payload_falls_back_and_backs_off(self):
        w = self.make()
        self.play()
        real_init = FakeRPC.__init__

        def patched(self_, cid, fail=None, pipe=0):
            real_init(self_, cid, fail, pipe)
            self_.fail_next = ServerError("bad image")

        FakeRPC.__init__ = patched
        try:
            await w._tick()
        finally:
            FakeRPC.__init__ = real_init
        kw = self.rpc.calls[-1][1]
        self.assertNotIn("large_image", kw)
        self.assertEqual((kw["details"], w.status.state), ("Song", "playing"))

    async def test_fully_rejected_status_is_not_hammered(self):
        w = self.make()
        self.play()
        await w._tick()
        rpc = self.rpc

        async def always_fail(**kw):
            rpc.attempts = getattr(rpc, "attempts", 0) + 1
            raise ServerError("nope")

        rpc.update = always_fail
        self.play("Other")
        for _ in range(6):
            self.clock.t += 3
            await w._tick()
        self.assertLessEqual(rpc.attempts, 4)                        # 2 попытки (полный+упрощённый), потом пауза

    async def test_slow_cover_arrives_later(self):
        class Slow(FakeResolver):
            def resolve(self, a, t, al):
                time.sleep(0.3)
                return super().resolve(a, t, al)

        rpcmod.COVER_WAIT, old = 0.05, rpcmod.COVER_WAIT
        self.addCleanup(lambda: setattr(rpcmod, "COVER_WAIT", old))
        w = self.make()
        w.resolver = Slow()
        self.play()
        await w._tick()
        self.assertNotIn("large_image", self.rpc.calls[0][1])
        await asyncio.sleep(0.5)
        self.clock.t += 3
        await w._tick()
        self.assertIn("large_image", self.rpc.calls[-1][1])

    async def test_thread_run_and_stop_clears_status(self):
        w = self.make()
        w._loop = w._wake = None
        self.play()
        self.store.update(poll_interval=1.0)
        th = threading.Thread(target=w.run_forever)
        th.start()
        await asyncio.sleep(1.3)
        self.assertEqual(len(w.list_sources().result(timeout=3)), 1)
        w.stop()
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertEqual(FakeRPC.instances[0].calls[-1][0], "clear")


class TogetherIntegrationTests(WorkerCase):
    def together_for(self, mode, name, code):
        t = Together(link_factory=self.bus.link_factory(), clock=self.clock)
        return t, dict(together_mode=mode, together_room=code, together_name=name)

    async def test_host_shares_even_without_discord(self):
        code = generate_code()
        host_t, cfg = self.together_for("host", "Аня", code)
        w = self.make(together=host_t)
        self.store.update(client_id="", **cfg)                       # Discord не настроен — трансляция всё равно идёт
        self.play("Shared", "Band")
        guest = Together(link_factory=self.bus.link_factory(), clock=self.clock)
        from ymrpc.config import Config
        guest.configure(Config(together_mode="guest", together_room=code))
        await w._tick()
        remote = guest.remote(self.clock())
        self.assertEqual((remote.title, remote.cover, remote.link), ("Shared", COVER.cover_url, COVER.track_url))

    async def test_hidden_track_is_not_shared(self):
        code = generate_code()
        host_t, cfg = self.together_for("host", "Аня", code)
        w = self.make(together=host_t)
        self.store.update(hide_keywords=["secret"], **cfg)
        from ymrpc.config import Config
        guest = Together(link_factory=self.bus.link_factory(), clock=self.clock)
        guest.configure(Config(together_mode="guest", together_room=code))
        self.play("Secret Song", "X")
        await w._tick()
        self.assertIsNone(guest.remote(self.clock()))

    async def test_guest_mirrors_host_in_discord(self):
        code = generate_code()
        from ymrpc.config import Config
        host = Together(link_factory=self.bus.link_factory(), clock=self.clock)
        host.configure(Config(together_mode="host", together_room=code, together_name="Аня"))
        from ymrpc.media import Track
        host.publish_local(Track("y", "Host Song", "Host Artist", "Alb", True, 50.0, 200.0),
                           "https://host/c.jpg", "https://music.yandex.ru/track/9", self.clock())

        guest_t, cfg = self.together_for("guest", "Боря", code)
        w = self.make(together=guest_t)
        self.store.update(**cfg)
        self.win.sessions[:] = []                                    # у гостя ничего не играет
        await w._tick()
        kw = self.rpc.calls[-1][1]
        self.assertEqual((kw["details"], kw["state"]), ("Host Song", "Host Artist · вместе с Аня"))
        self.assertEqual(kw["large_image"], "https://host/c.jpg")
        self.assertEqual(w.current()[2], "Аня")
        self.assertEqual(w.history.items()[0].title, "Host Song")

        self.store.update(together_mirror=False)                     # без зеркала — обычный статус (пусто)
        self.clock.t += 3
        await w._tick()                                              # трека нет — ждём «льготные» секунды
        self.clock.t += rpcmod.NONE_GRACE + 1
        await w._tick()
        self.assertEqual(self.rpc.calls[-1][0], "clear")

    async def test_guest_player_is_synced_when_same_track(self):
        code = generate_code()
        from ymrpc.config import Config
        from ymrpc.media import Track
        host = Together(link_factory=self.bus.link_factory(), clock=self.clock)
        host.configure(Config(together_mode="host", together_room=code))
        host.publish_local(Track("y", "Song", "Artist", "", True, 100.0, 180.0), None, None, self.clock())

        guest_t, cfg = self.together_for("guest", "Боря", code)
        w = self.make(together=guest_t)
        self.store.update(**cfg)
        s = self.play("Song", "Artist", status=PAUSED, pos=3)         # у гостя тот же трек, но на паузе и в другом месте
        await w._tick()
        self.assertEqual(s.commands, [("play",)])
        self.clock.t += 5
        host.publish_local(Track("y", "Song", "Artist", "", True, 105.0, 180.0), None, None, self.clock())
        await w._tick()
        self.assertEqual(s.commands[-1][0], "seek")
        self.assertAlmostEqual(s.commands[-1][1] / 1e7, 105.3, delta=1.0)

        self.store.update(together_sync_player=False)
        s.commands.clear()
        self.clock.t += 20
        s._info.playback_status = PAUSED
        host.publish_local(Track("y", "Song", "Artist", "", True, 130.0, 180.0), None, None, self.clock())
        await w._tick()
        self.assertEqual(s.commands, [])                              # синхронизация выключена — плеер не трогаем


if __name__ == "__main__":
    unittest.main()
