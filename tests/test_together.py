import unittest

from tests.helpers import Clock, MemoryBus
from ymrpc.config import Config
from ymrpc.media import Track
from ymrpc.together import (
    RemoteState, SyncState, Together, derive_room, generate_code, normalize_code, parse_broker,
    broker_list, plan_sync, open_url, selfcheck, _safe_url, _num, LINK_HOSTS,
    HEARTBEAT, STALE_AFTER, SYNC_COOLDOWN, SEEK_COOLDOWN,
)


def track(title="Song", artist="Artist", playing=True, pos=40.0, dur=200.0):
    return Track("yandex", title, artist, "Album", playing, pos, dur)


class CodeAndCryptoTests(unittest.TestCase):
    def test_codes(self):
        code = generate_code()
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertNotIn("O", code.replace("-", ""))
        self.assertEqual(normalize_code(code.lower().replace("-", " ")), code)
        for bad in ("", "abc", "ABCD-EFGH-JKL", "ABCD-EFGH-JKL0", "ABCD-EFGH-JKLMN", None):
            self.assertIsNone(normalize_code(bad), bad)
        self.assertNotEqual(generate_code(), generate_code())

    def test_room_derivation(self):
        a, b = derive_room("ABCD-EFGH-JKLM"), derive_room("abcd efgh jklm")
        self.assertEqual((a.host_topic, a.guest_topic), (b.host_topic, b.guest_topic))
        other = derive_room("ABCD-EFGH-JKLN")
        self.assertNotEqual(a.host_topic, other.host_topic)
        self.assertTrue(a.host_topic.startswith("musicrpc/v1/") and a.host_topic.endswith("/host"))
        self.assertNotIn("ABCD", a.host_topic)          # в имени канала кода нет
        with self.assertRaises(ValueError):
            derive_room("nope")

    def test_box_roundtrip_and_attacks(self):
        room, other = derive_room("ABCD-EFGH-JKLM"), derive_room("ABCD-EFGH-JKLN")
        msg = {"title": "Песня — 🎵", "n": 1}
        sealed = room.box.seal(msg)
        self.assertNotIn("Песня".encode(), sealed)
        self.assertEqual(room.box.open(sealed), msg)
        self.assertIsNone(other.box.open(sealed))                       # чужая комната
        self.assertNotEqual(room.box.seal(msg), room.box.seal(msg))     # разные nonce
        tampered = bytearray(sealed)
        tampered[len(tampered) // 2] ^= 1
        self.assertIsNone(room.box.open(bytes(tampered)))               # подделка
        for junk in (b"", b"!!!", b"AAAA", sealed[:20], b"\x00" * 100):
            self.assertIsNone(room.box.open(junk))

    def test_large_and_empty_payloads(self):
        box = derive_room("ABCD-EFGH-JKLM").box
        big = {"x": "я" * 5000}
        self.assertEqual(box.open(box.seal(big)), big)
        self.assertEqual(box.open(box.seal({})), {})

    def test_helpers(self):
        self.assertEqual(parse_broker("mqtt://h.example:1884"), ("h.example", 1884, False))
        self.assertEqual(parse_broker("mqtts://h.example"), ("h.example", 8883, True))
        self.assertEqual(parse_broker("h.example"), ("h.example", 1883, False))
        for bad in ("http://x", "mqtt://"):
            with self.assertRaises(ValueError):
                parse_broker(bad)
        self.assertEqual(len(broker_list("")), 2)
        self.assertEqual(broker_list("a:1, b:2"), ["a:1", "b:2"])
        self.assertEqual(_safe_url("javascript:alert(1)"), "")
        self.assertEqual(_safe_url("file:///c:/x"), "")
        self.assertEqual(_safe_url("https://a/b"), "https://a/b")
        self.assertEqual(_safe_url("https://music.yandex.ru/a", hosts=LINK_HOSTS), "https://music.yandex.ru/a")
        for evil in ("https://evil.example/x", "https://evilyandex.ru/x", "https://yandex.ru.evil.example/x"):
            self.assertEqual(_safe_url(evil, hosts=LINK_HOSTS), "", evil)
        self.assertEqual(_num("nan", 7), 7)
        self.assertEqual(_num(1.7e9), 1.7e9)     # временные метки Unix должны проходить
        self.assertFalse(open_url("file:///etc/passwd"))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.clock = MemoryBus(), Clock(1000.0)
        self.code = generate_code()
        self.events = []

    def peer(self, mode, name):
        p = Together(on_change=lambda: self.events.append(name), link_factory=self.bus.link_factory(), clock=self.clock)
        p.configure(Config(together_mode=mode, together_room=self.code, together_name=name))
        return p

    def test_host_to_guest_and_position_extrapolation(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), "https://img/c.jpg", "https://music.yandex.ru/track/1", self.clock())
        r = guest.remote(self.clock())
        self.assertEqual((r.title, r.artist, r.host_name, r.playing), ("Song", "Artist", "Аня", True))
        self.assertEqual((r.cover, r.link), ("https://img/c.jpg", "https://music.yandex.ru/track/1"))
        self.assertAlmostEqual(r.position_now(self.clock() + 5), 45.0)
        self.assertAlmostEqual(r.as_track(self.clock() + 500).position, 200.0)   # не дальше конца трека

    def test_link_from_foreign_site_is_dropped(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), "https://img/c.jpg", "https://evil.example/phish", self.clock())
        r = guest.remote(self.clock())
        self.assertEqual((r.cover, r.link), ("https://img/c.jpg", ""))

    def test_late_guest_gets_retained_state(self):
        host = self.peer("host", "Аня")
        host.publish_local(track(), None, None, self.clock())
        guest = self.peer("guest", "Боря")
        self.assertEqual(guest.remote(self.clock()).title, "Song")

    def test_change_pause_and_seek_are_published(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), None, None, self.clock())
        self.clock.t += 2
        host.publish_local(track(pos=42.0), None, None, self.clock())        # то же самое — без нового сообщения
        self.assertEqual(len(host._link.published), 1)
        self.clock.t += 2
        host.publish_local(track(playing=False, pos=44.0), None, None, self.clock())
        self.assertFalse(guest.remote(self.clock()).playing)
        self.clock.t += 2
        host.publish_local(track(playing=True, pos=120.0), None, None, self.clock())   # перемотка
        self.assertAlmostEqual(guest.remote(self.clock()).position, 120.0)
        n = len(host._link.published)
        self.clock.t += HEARTBEAT + 1
        host.publish_local(track(pos=134.0), None, None, self.clock())        # «пульс»
        self.assertEqual(len(host._link.published), n + 1)

    def test_nothing_playing_makes_remote_inactive(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), None, None, self.clock())
        self.clock.t += 2
        host.publish_local(None, None, None, self.clock())
        self.assertIsNone(guest.remote(self.clock()))

    def test_host_leaving_and_crash(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), None, None, self.clock())
        host.stop()                                        # корректный выход
        self.assertIsNone(guest.remote(self.clock()))
        self.assertNotIn(derive_room(self.code).host_topic, self.bus.retained)   # сохранённое состояние очищено

        host2 = self.peer("host", "Аня")
        host2.publish_local(track(), None, None, self.clock())
        self.assertIsNotNone(guest.remote(self.clock()))
        host2._link.crash()                                # обрыв без предупреждения: сработает «завещание»
        self.assertIsNone(guest.remote(self.clock()))

    def test_stale_host_is_dropped(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), None, None, self.clock())
        self.clock.t += STALE_AFTER + 1
        self.assertIsNone(guest.remote(self.clock()))

    def test_guest_count_for_host(self):
        host = self.peer("host", "Аня")
        g1, g2 = self.peer("guest", "Боря"), self.peer("guest", "Вера")
        g1.tick(self.clock())
        g2.tick(self.clock())
        self.assertEqual(host.guests(self.clock()), ["Боря", "Вера"])
        self.assertIn("слушают: 2", host.describe(self.clock()))
        self.clock.t += 100
        self.assertEqual(host.guests(self.clock()), [])

    def test_other_room_and_second_host(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        stranger = Together(link_factory=self.bus.link_factory(), clock=self.clock)
        stranger.configure(Config(together_mode="guest", together_room=generate_code()))
        host.publish_local(track(), None, None, self.clock())
        self.assertIsNone(stranger.remote(self.clock()))
        host_b = self.peer("host", "Влад")                 # второй хост в той же комнате
        self.clock.t += 2
        host_b.publish_local(track("Другая"), None, None, self.clock())
        self.assertEqual(guest.remote(self.clock()).host_name, "Аня")

    def test_bad_clock_and_replay_are_rejected(self):
        host, guest = self.peer("host", "Аня"), self.peer("guest", "Боря")
        host.publish_local(track(), None, None, self.clock())
        old = host._link.published[0][1]
        guest._remote = None
        self.clock.t += 3600                               # «повтор» старого сообщения через час
        guest._on_message(guest._gen, guest._room.host_topic, old, False)
        self.assertIsNone(guest.remote(self.clock()))

    def test_switching_mode_reconnects_and_off_disconnects(self):
        guest = self.peer("guest", "Боря")
        self.assertEqual(guest.mode, "guest")
        guest.configure(Config(together_mode="off", together_room=self.code))
        self.assertEqual((guest.mode, guest.connection), ("off", "off"))
        self.assertEqual(guest.describe(), "Слушать вместе: выключено")
        guest.configure(Config(together_mode="guest", together_room="broken"))   # плохой код = выключено
        self.assertEqual(guest.mode, "off")

    def test_missing_mqtt_package_is_explained(self):
        class NoPaho:
            def __init__(self, brokers, sub, will, on_message, on_status):
                self.on_status = on_status

            def start(self):
                self.on_status("missing", "paho-mqtt")

            def stop(self):
                pass

        t = Together(link_factory=NoPaho, clock=self.clock)
        t.configure(Config(together_mode="guest", together_room=self.code))
        self.assertIn("install.bat", t.describe())

    def test_listeners_are_called(self):
        self.peer("host", "Аня")
        self.assertTrue(self.events)


class SelfCheckTests(unittest.TestCase):
    def test_passes_through_a_working_broker(self):
        ok, lines = selfcheck(link_factory=MemoryBus().link_factory(), connect_timeout=2, receive_timeout=2)
        self.assertTrue(ok, lines)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith("✓") for line in lines), lines)

    def test_reports_unreachable_server(self):
        class Dead:
            def __init__(self, *a): pass
            def start(self): pass
            def stop(self): pass

        ok, lines = selfcheck(link_factory=Dead, connect_timeout=0.3)
        self.assertFalse(ok)
        self.assertTrue(lines[0].startswith("✗ не удалось подключиться"))

    def test_reports_messages_that_do_not_arrive(self):
        class Deaf(MemoryBus):
            def deliver(self, topic, payload, retain, origin=None):
                pass                                    # сервер принимает, но ничего не доставляет

        ok, lines = selfcheck(link_factory=Deaf().link_factory(), connect_timeout=1, receive_timeout=0.3)
        self.assertFalse(ok)
        self.assertIn("не получил", lines[-1] + lines[-2])

    def test_missing_paho_is_explained(self):
        import sys
        saved = {k: v for k, v in sys.modules.items() if k.startswith("paho")}
        for k in list(saved):
            sys.modules.pop(k)
        sys.modules["paho"] = None
        try:
            ok, lines = selfcheck()
        finally:
            sys.modules.pop("paho", None)
            sys.modules.update(saved)
        self.assertFalse(ok)
        self.assertIn("install.bat", lines[0])


class PlanSyncTests(unittest.TestCase):
    def remote(self, **kw):
        base = dict(host_id="h", host_name="Аня", active=True, playing=True, title="Song", artist="Artist",
                    album="", position=60.0, duration=200.0, cover="", link="https://l", received=1000.0)
        base.update(kw)
        return RemoteState(**base)

    def plan(self, local, remote, now=1000.0, st=None, sync=True, auto=False):
        return plan_sync(local, remote, now, st or SyncState(), sync, auto)

    def test_nothing_to_do(self):
        self.assertEqual(self.plan(track(pos=60), self.remote()), [])
        self.assertEqual(self.plan(track(), None), [])
        self.assertEqual(self.plan(track(), self.remote(active=False)), [])
        self.assertEqual(self.plan(None, self.remote()), [])

    def test_play_pause(self):
        self.assertEqual(self.plan(track(playing=False, pos=60), self.remote()), [("play",)])
        self.assertEqual(self.plan(track(playing=True, pos=60), self.remote(playing=False)), [("pause",)])
        self.assertEqual(self.plan(track(playing=False), self.remote(playing=False)), [])

    def test_guests_manual_pause_is_respected(self):
        st = SyncState()
        r = self.remote()
        self.assertEqual(self.plan(track(playing=False, pos=60), r, st=st), [("play",)])       # первое совпадение
        self.assertEqual(self.plan(track(playing=False, pos=60), r, now=1010.0, st=st), [])    # гость сам нажал паузу — не спорим
        self.assertEqual(self.plan(track(playing=False, pos=60), r, now=1020.0, st=st), [])
        paused = self.remote(playing=False)
        self.assertEqual(self.plan(track(playing=False, pos=60), paused, now=1030.0, st=st), [])   # хост тоже на паузе
        resumed = self.remote(playing=True, received=1040.0)
        self.assertEqual(self.plan(track(playing=False, pos=60), resumed, now=1040.0, st=st), [("play",)])  # хост нажал плей
        self.assertEqual(self.plan(track(playing=True, pos=60), self.remote(playing=False), now=1050.0, st=st),
                         [("pause",)])

    def test_new_host_track_realigns(self):
        st = SyncState()
        self.assertEqual(self.plan(track(playing=False, pos=60), self.remote(), st=st), [("play",)])
        nxt = self.remote(title="Next", received=1100.0, position=0.0)
        local = track("Next", playing=False, pos=0.0)
        self.assertEqual(self.plan(local, nxt, now=1100.0, st=st), [("play",)])

    def test_seek_only_when_far_and_cooldown(self):
        st = SyncState()
        self.assertEqual(self.plan(track(pos=62.0), self.remote(), st=st), [])            # 2 с — терпимо
        self.assertEqual(self.plan(track(pos=20.0), self.remote(), st=st), [("seek", 60.3)])
        st.last_seek = 1000.0
        self.assertEqual(self.plan(track(pos=20.0), self.remote(), now=1000.0 + SEEK_COOLDOWN - 1, st=st), [])
        st.seek_ok = False
        self.assertEqual(self.plan(track(pos=20.0), self.remote(), now=2000.0, st=st), [])

    def test_seek_target_moves_with_time(self):
        self.assertEqual(self.plan(track(pos=20.0), self.remote(), now=1010.0), [("seek", 70.3)])

    def test_command_cooldown(self):
        st = SyncState(last_cmd=1000.0)
        self.assertEqual(self.plan(track(playing=False), self.remote(), now=1000.0 + SYNC_COOLDOWN - 1, st=st), [])
        self.assertEqual(self.plan(track(playing=False), self.remote(), now=1000.0 + SYNC_COOLDOWN + 1, st=st), [("play",)])

    def test_different_track_never_touches_player(self):
        other = track("Другой трек", "Другой")
        self.assertEqual(self.plan(other, self.remote()), [])
        self.assertEqual(self.plan(other, self.remote(), sync=False), [])

    def test_auto_open_once(self):
        st = SyncState()
        other = track("Другой трек", "Другой")
        r = self.remote()
        self.assertEqual(self.plan(other, r, st=st, auto=True), [("open", "https://l")])
        st.opened_key = r.key
        self.assertEqual(self.plan(other, r, st=st, auto=True), [])
        self.assertEqual(self.plan(None, self.remote(link=""), auto=True), [])              # ссылки нет
        self.assertEqual(self.plan(track(pos=60), self.remote(), auto=True), [])          # тот же трек — не открываем


if __name__ == "__main__":
    unittest.main()
