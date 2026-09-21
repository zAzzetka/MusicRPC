import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import FakeSession, FakeWinrt  # noqa: F401  (добавляет корень проекта в sys.path)
from ymrpc.config import Config, ConfigStore, sanitize
from ymrpc.covers import CoverResolver, Resolved
from ymrpc.history import History
from ymrpc.media import MediaReader, Track, matches, read_timeline
from ymrpc.rpc import build_activity, fit, is_hidden, render
from ymrpc.textmatch import artist_ok, same_track, title_ok

from pypresence import ActivityType


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_roundtrip_and_clamping(self):
        s = ConfigStore(self.dir / "c.json")
        self.assertTrue(s.first_run)
        s.update(client_id=" 123456789012345678 ", poll_interval=99, show_cover=False)
        c = ConfigStore(self.dir / "c.json").get()
        self.assertEqual((c.client_id, c.poll_interval, c.show_cover), ("123456789012345678", 10.0, False))

    def test_broken_and_wrong_types(self):
        (self.dir / "bad.json").write_text("{not json")
        self.assertTrue(ConfigStore(self.dir / "bad.json").get().enabled)
        (self.dir / "t.json").write_text(json.dumps(
            {"enabled": "yes", "poll_interval": "x", "app_filters": "a, b", "status_display": "zzz",
             "together_mode": "root", "together_room": "bad", "hide_keywords": "x, y"}))
        c = ConfigStore(self.dir / "t.json").get()
        self.assertEqual((c.enabled, c.poll_interval, c.app_filters, c.status_display), (True, 2.0, ["a", "b"], "name"))
        self.assertEqual((c.together_mode, c.together_room, c.hide_keywords), ("off", "", ["x", "y"]))

    def test_room_code_is_normalized(self):
        c = sanitize(Config(together_room="abcd efgh-jkl m", together_mode="host"))
        self.assertEqual((c.together_room, c.together_mode), ("ABCD-EFGH-JKLM", "host"))

    def test_legacy_folder_is_migrated(self):
        import os
        os.environ["APPDATA"] = str(self.dir)
        old = self.dir / "YandexMusicRPC"
        old.mkdir()
        (old / "config.json").write_text(json.dumps({"client_id": "42"}))
        from ymrpc.config import data_dir
        self.assertEqual(data_dir(), self.dir / "MusicRPC")
        self.assertEqual(json.loads((self.dir / "MusicRPC" / "config.json").read_text())["client_id"], "42")


class TextTests(unittest.TestCase):
    def test_render(self):
        v = {"title": "Song", "artist": "", "album": ""}
        self.assertEqual(render("{artist} — {title}", v), "Song")
        self.assertEqual(render("{artist} — {title}", {**v, "artist": "A"}), "A — Song")
        self.assertEqual(render("{oops}", v), "{oops}")
        self.assertEqual(render("{bad", v), "{bad")
        self.assertEqual(render("{title.__class__}", v), "<class 'str'>")  # пользовательский шаблон не опасен

    def test_fit(self):
        self.assertEqual(fit("a"), "a\u200b")
        self.assertEqual(len(fit("x" * 300)), 120)
        self.assertEqual(fit("  "), "")

    def test_matching(self):
        self.assertTrue(title_ok("Song (feat. X)", "Song"))
        self.assertFalse(title_ok("Abc Def", "Xyz Qwe"))
        self.assertTrue(artist_ok("Artist1, Artist2", ["Artist2"]))
        self.assertFalse(artist_ok("Foo", ["Bar"]))
        self.assertTrue(same_track("Кино", "Ё", "кино", "е"))
        self.assertFalse(same_track("Song", "A", "Song", "B"))
        self.assertTrue(same_track("Song", "", "Song", "B"))

    def test_hidden(self):
        t = Track("Yandex.Music", "Guilty Pleasure", "Some Band", "Album", True, 0, 0)
        self.assertTrue(is_hidden(t, ["guilty"]))
        self.assertTrue(is_hidden(t, ["  BAND "]))
        self.assertTrue(is_hidden(t, ["yandex"]))
        self.assertFalse(is_hidden(t, ["other", ""]))
        self.assertFalse(is_hidden(t, []))

    def test_matches_filters(self):
        self.assertTrue(matches("ru.yandex.desktop.music", ["yandex"]))
        self.assertFalse(matches("chrome.exe", ["yandex"]))
        self.assertTrue(matches("chrome.exe", []))


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(client_id="1")
        self.res = Resolved("https://img/x.jpg", "https://music.yandex.ru/album/1/track/2", "y")
        self.t = Track("y", "Title", "Artist", "Album", True, 30.0, 200.0)

    def test_full(self):
        a = build_activity(self.t, self.cfg, self.res, 1000.0)
        self.assertEqual(a.static["activity_type"], ActivityType.LISTENING)
        self.assertEqual((a.static["details"], a.static["state"], a.static["large_text"]), ("Title", "Artist", "Album"))
        self.assertEqual(a.static["buttons"][0]["label"], "Открыть трек")
        self.assertEqual((a.start, a.end), (970, 1170))

    def test_switches(self):
        cfg = Config(show_cover=False, show_button=False, show_progress=False)
        a = build_activity(self.t, cfg, self.res, 1000.0)
        self.assertNotIn("large_image", a.static)
        self.assertNotIn("buttons", a.static)
        self.assertIsNone(a.start)

    def test_paused_and_together(self):
        paused = Track("y", "Title", "Artist", "Album", False, 30.0, 200.0)
        a = build_activity(paused, self.cfg, self.res, 1000.0, together_name="Аня")
        self.assertEqual(a.static["state"], "⏸ Artist · вместе с Аня")
        self.assertIsNone(a.start)

    def test_own_timer_when_player_gives_no_position(self):
        t = Track("y", "T", "A", "", True, 0.0, 0.0)
        a = build_activity(t, self.cfg, Resolved(), 1000.0, fallback_start=990)
        self.assertEqual((a.start, a.end), (990, None))


class CoverTests(unittest.TestCase):
    @staticmethod
    def fetcher(routes):
        def fetch(url, params, headers):
            value = routes[url]
            if isinstance(value, Exception):
                raise value
            return value
        return fetch

    YANDEX = {"result": {"tracks": {"results": [
        {"id": "9", "title": "Wrong", "artists": [{"name": "Nobody"}], "albums": [{"id": 1}]},
        {"id": "5", "realId": "5", "title": "Song", "artists": [{"name": "Artist"}],
         "albums": [{"id": 77, "coverUri": "a.yandex.net/x/%%"}], "coverUri": "a.yandex.net/x/%%"}]}}}

    def test_yandex_then_fallbacks(self):
        r = CoverResolver(fetch=self.fetcher({"https://api.music.yandex.net/search": self.YANDEX}))
        out = r.resolve("Artist", "Song")
        self.assertEqual(out.cover_url, "https://a.yandex.net/x/400x400")
        self.assertEqual(out.track_url, "https://music.yandex.ru/album/77/track/5")
        self.assertIs(r.peek("Artist", "Song"), out)

        r = CoverResolver(fetch=self.fetcher({
            "https://api.music.yandex.net/search": RuntimeError("blocked"),
            "https://api.deezer.com/search": {"data": [{"title": "Song", "artist": {"name": "Artist"},
                                                        "album": {"cover_xl": "https://dz/c.jpg"}}]}}))
        out = r.resolve("Artist", "Song")
        self.assertEqual((out.cover_url, out.track_url, out.source), ("https://dz/c.jpg", None, "deezer"))

    def test_itunes_artwork_is_upscaled(self):
        r = CoverResolver(fetch=self.fetcher({
            "https://api.music.yandex.net/search": RuntimeError("x"),
            "https://api.deezer.com/search": {"data": []},
            "https://itunes.apple.com/search": {"results": [
                {"trackName": "Song", "artistName": "Artist", "artworkUrl100": "https://a/100x100bb.jpg"}]}}))
        self.assertEqual(r.resolve("Artist", "Song").cover_url, "https://a/600x600bb.jpg")

    def test_everything_down_never_raises(self):
        urls = ["https://api.music.yandex.net/search", "https://api.deezer.com/search", "https://itunes.apple.com/search"]
        r = CoverResolver(fetch=self.fetcher({u: RuntimeError("x") for u in urls}))
        self.assertFalse(r.resolve("A", "B").found)

    def test_http_get_json_uses_stdlib(self):
        import http.server
        import threading
        from ymrpc.covers import http_get_json

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = 404 if "missing" in self.path else 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps({"path": self.path, "ua": self.headers["User-Agent"]}).encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        data = http_get_json(base + "/s", {"q": "Кино & Ко", "n": 1})
        self.assertIn("q=%D0%9A%D0%B8%D0%BD%D0%BE+%26+%D0%9A%D0%BE", data["path"])
        self.assertTrue(data["ua"].startswith("MusicRPC"))
        with self.assertRaises(Exception):
            http_get_json(base + "/missing")               # 404 — исключение


class TimelineTests(unittest.TestCase):
    NOW = datetime(2026, 1, 1, 12, 0, 20, tzinfo=timezone.utc)

    class TL:
        start_time, end_time, position = timedelta(0), timedelta(seconds=200), timedelta(seconds=10)
        last_updated_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class Sess:
        def __init__(self, tl): self.tl = tl
        def get_timeline_properties(self): return self.tl()

    def test_extrapolation(self):
        pos, dur = read_timeline(self.Sess(self.TL), True, self.NOW)
        self.assertEqual((round(pos), dur), (30, 200))
        self.assertEqual(round(read_timeline(self.Sess(self.TL), False, self.NOW)[0]), 10)

    def test_no_info(self):
        class TL2(self.TL):
            end_time = timedelta(0)
            last_updated_time = datetime(1601, 1, 1)
        self.assertEqual(read_timeline(self.Sess(TL2), True, self.NOW), (10.0, 0.0))


class HistoryTests(unittest.TestCase):
    def test_dedupe_limit_and_link(self):
        h = History(maxlen=3)
        h.add("A", "One")
        h.add("A", "One", "https://x/1")          # повтор не дублируется, но ссылку дописывает
        h.add("B", "Two")
        h.set_link("B", "Two", "https://x/2")
        h.add("C", "Three")
        h.add("D", "Four")                          # вытесняет самый старый
        items = h.items()
        self.assertEqual([i.title for i in items], ["Four", "Three", "Two"])
        self.assertEqual(items[2].link, "https://x/2")


class MediaReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.win = FakeWinrt().install()
        self.addCleanup(self.win.uninstall)

    async def test_selection_rules(self):
        r = MediaReader()
        chrome = FakeSession("chrome.exe", "Video", "Chan", "", 4)
        ym = FakeSession("ru.yandex.desktop.music", "Song", "Artist", "Album", 5)
        other = FakeSession("Yandex.Browser", "Other", "X", "", 4)
        self.win.sessions[:] = [chrome, ym, other]
        self.assertEqual((await r.read(["yandex"])).title, "Other")     # играющий приоритетнее
        self.win.sessions.remove(other)
        t = await r.read(["yandex"])
        self.assertEqual((t.title, t.playing), ("Song", False))
        self.assertIsNone(await r.read(["nothing"]))
        self.assertEqual((await r.read([])).title, "Video")

    async def test_prefers_windows_current_session(self):
        a = FakeSession("a.exe", "A", "x", "", 4)
        b = FakeSession("b.exe", "B", "x", "", 4)
        self.win.sessions[:] = [a, b]
        self.win.current = b
        self.assertEqual((await MediaReader().read([])).title, "B")

    async def test_empty_titles_are_skipped(self):
        self.win.sessions[:] = [FakeSession("x", "", "", "", 4)]
        r = MediaReader()
        self.assertIsNone(await r.read([]))
        self.assertEqual(len(await r.list_sources()), 1)

    async def test_commands(self):
        s = FakeSession("yandex", "Song", "Artist", "", 5)
        self.win.sessions[:] = [s]
        r = MediaReader()
        self.assertTrue(await r.send_command(["yandex"], "play"))
        self.assertTrue(await r.send_command(["yandex"], "seek", 12.5))
        self.assertEqual(s.commands, [("play",), ("seek", 125_000_000)])
        self.assertFalse(await r.send_command(["nothing"], "play"))

    async def test_commands_respect_player_capabilities(self):
        from tests.helpers import FakeControls
        s = FakeSession("yandex", "Song", "Artist", "", 4, controls=FakeControls(position=False))
        self.win.sessions[:] = [s]
        self.assertFalse(await MediaReader().send_command(["yandex"], "seek", 5))
        self.assertEqual(s.commands, [])

    async def test_missing_winrt_gives_readable_error(self):
        import sys
        self.win.uninstall()
        for n in ("winrt", "winrt.windows", "winrt.windows.media", "winrt.windows.media.control"):
            sys.modules[n] = None   # имитируем ImportError
        try:
            with self.assertRaisesRegex(RuntimeError, "install.bat"):
                await MediaReader().read([])
        finally:
            for n in ("winrt", "winrt.windows", "winrt.windows.media", "winrt.windows.media.control"):
                sys.modules.pop(n, None)
            self.win.install()


if __name__ == "__main__":
    unittest.main()
