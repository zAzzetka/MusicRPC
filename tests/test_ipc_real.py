"""Настоящий pypresence.AioPresence против поддельного Discord (unix-сокет). На Windows пропускается."""
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import Clock
from ymrpc.config import ConfigStore
from ymrpc.covers import Resolved
from ymrpc.media import Track
from ymrpc.rpc import Worker, open_presence, _default_rpc_factory
from ymrpc.status import Status


def frame(op, obj):
    b = json.dumps(obj).encode()
    return struct.pack("<II", op, len(b)) + b


@unittest.skipIf(sys.platform == "win32", "unix-сокеты: на Windows Discord использует именованные каналы")
class RealPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp
        self.received, self.servers = [], []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for srv in self.servers:
            srv.close()
        if self._old is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._old

    async def serve(self, mode, idx=0):
        received = self.received

        async def handle(reader, writer):
            try:
                hdr = await reader.readexactly(8)
            except Exception:
                writer.close()
                return
            _op, n = struct.unpack("<II", hdr)
            await reader.readexactly(n)
            if mode == "hang":
                await asyncio.sleep(3600)
                return
            if mode == "badid":
                writer.write(frame(1, {"code": 4000, "message": "Invalid Client ID"}))
                await writer.drain()
                return
            writer.write(frame(1, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1}}))
            await writer.drain()
            while True:
                try:
                    _op, n = struct.unpack("<II", await reader.readexactly(8))
                    msg = json.loads(await reader.readexactly(n))
                except Exception:
                    return
                received.append(msg)
                writer.write(frame(1, {"cmd": msg["cmd"], "evt": None, "data": {}, "nonce": msg["nonce"]}))
                await writer.drain()

        path = os.path.join(self.tmp, f"discord-ipc-{idx}")
        if os.path.exists(path):
            os.remove(path)
        srv = await asyncio.start_unix_server(handle, path=path)
        self.servers.append(srv)
        return srv

    def worker(self):
        class Reader:
            async def read(self, filters):
                return Track("player", "Song", "Artist", "Album", True, 10.0, 180.0)

        class Resolver:
            def peek(self, a, t):
                return Resolved("https://c/x.jpg", "https://music.yandex.ru/track/1", "y")

        store = ConfigStore(Path(tempfile.mkdtemp()) / "c.json")
        store.update(client_id="123456789012345678")
        clock = Clock()
        w = Worker(store, Status(), reader=Reader(), resolver=Resolver(), clock=clock,
                   together=__import__("ymrpc.together", fromlist=["Together"]).Together(clock=clock))
        w._loop, w._wake = asyncio.get_running_loop(), asyncio.Event()
        return w, clock

    async def test_normal_channel(self):
        await self.serve("ok")
        w, _ = self.worker()
        await asyncio.wait_for(w._tick(), 15)
        self.assertEqual(w.status.state, "playing")
        self.assertEqual([m["cmd"] for m in self.received], ["SET_ACTIVITY"])
        await w._shutdown()

    async def test_silent_channel_does_not_freeze_and_live_one_is_found(self):
        await self.serve("hang", 0)
        await self.serve("ok", 1)
        rpc, errors = await open_presence(_default_rpc_factory, "123456789012345678", timeout=1.0)
        self.assertIsNotNone(rpc)                       # канал 0 молчит, канал 1 живой
        self.assertEqual(len(errors), 1)

    async def test_late_discord_start(self):
        s0 = await self.serve("hang", 0)                # Discord ещё запускается: канал есть, но молчит
        w, clock = self.worker()
        import ymrpc.rpc as rpcmod
        rpcmod.CONNECT_TIMEOUT = 1.0
        original = rpcmod.open_presence
        rpcmod.open_presence = lambda f, c: original(f, c, timeout=1.0)
        self.addCleanup(lambda: setattr(rpcmod, "open_presence", original))
        t0 = time.time()
        await asyncio.wait_for(w._tick(), 20)
        self.assertEqual(w.status.state, "discord_error")
        self.assertLess(time.time() - t0, 10)
        s0.close()
        try:                                            # начиная с Python 3.13 close() сам удаляет файл сокета
            os.remove(os.path.join(self.tmp, "discord-ipc-0"))
        except FileNotFoundError:
            pass
        await self.serve("ok", 0)                       # Discord «ожил»
        clock.t += 11
        await asyncio.wait_for(w._tick(), 15)
        self.assertEqual(w.status.state, "playing")
        await w._shutdown()

    async def test_no_discord_and_bad_id(self):
        w, _ = self.worker()
        await asyncio.wait_for(w._tick(), 15)
        self.assertEqual(w.status.state, "discord_missing")
        await self.serve("badid")
        w2, _ = self.worker()
        await asyncio.wait_for(w2._tick(), 15)
        self.assertEqual(w2.status.state, "bad_client_id")


if __name__ == "__main__":
    unittest.main()
