"""Together через настоящую библиотеку paho и настоящий брокер Mosquitto. Без mosquitto/paho — пропускается."""
import shutil
import socket
import subprocess
import time
import unittest

from ymrpc.config import Config
from ymrpc.media import Track
from ymrpc.together import Together, generate_code, selfcheck

try:
    import paho.mqtt.client  # noqa: F401
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False


def wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(shutil.which("mosquitto") and HAVE_PAHO, "нужны mosquitto и paho-mqtt")
class MqttTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen(["mosquitto", "-p", str(cls.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait(lambda: port_open(cls.port), 5), "брокер не запустился"
        cls.broker = f"mqtt://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def cfg(self, mode, code, name, broker=None):
        return Config(together_mode=mode, together_room=code, together_name=name, together_broker=broker or self.broker)

    def test_full_scenario(self):
        code = generate_code()
        host, guest = Together(), Together()
        self.addCleanup(host.stop)
        self.addCleanup(guest.stop)
        host.configure(self.cfg("host", code, "Аня"))
        self.assertTrue(wait(lambda: host.connection == "online"))
        host.publish_local(Track("y", "Song", "Artist", "Album", True, 12.0, 180.0),
                           "https://img/x.jpg", "https://music.yandex.ru/track/1", time.time())

        # гость приходит позже и сразу получает сохранённое (retained) состояние
        guest.configure(self.cfg("guest", code, "Боря"))
        self.assertTrue(wait(lambda: guest.remote(time.time()) is not None), guest.describe())
        self.assertEqual(guest.remote(time.time()).title, "Song")

        time.sleep(1.1)
        host.publish_local(Track("y", "Other", "B", "", False, 5.0, 100.0), None, None, time.time())
        self.assertTrue(wait(lambda: getattr(guest.remote(time.time()), "title", None) == "Other"))
        self.assertFalse(guest.remote(time.time()).playing)

        guest.tick(time.time() + 100)
        self.assertTrue(wait(lambda: host.guests() == ["Боря"]))

        # чужая комната ничего не видит
        stranger = Together()
        self.addCleanup(stranger.stop)
        stranger.configure(self.cfg("guest", generate_code(), "Чужой"))
        self.assertTrue(wait(lambda: stranger.connection == "online"))
        time.sleep(1.0)
        self.assertIsNone(stranger.remote(time.time()))

        # обрыв хоста без предупреждения — брокер публикует «завещание»
        host._link._client.socket().close()
        self.assertTrue(wait(lambda: guest.remote(time.time()) is None, 15), "завещание не сработало")

    def test_clean_exit_clears_state(self):
        code = generate_code()
        host, guest = Together(), Together()
        self.addCleanup(guest.stop)
        host.configure(self.cfg("host", code, "Аня"))
        self.assertTrue(wait(lambda: host.connection == "online"))
        host.publish_local(Track("y", "Song", "A", "", True, 1.0, 100.0), None, None, time.time())
        host.stop()
        late = Together()
        self.addCleanup(late.stop)
        late.configure(self.cfg("guest", code, "Поздний"))
        self.assertTrue(wait(lambda: late.connection == "online"))
        time.sleep(1.0)
        self.assertIsNone(late.remote(time.time()))          # сохранённого состояния больше нет

    def test_selfcheck_on_a_real_broker(self):
        ok, lines = selfcheck(self.broker)
        self.assertTrue(ok, lines)
        self.assertIn(f"127.0.0.1:{self.port}", lines[0])
        self.assertEqual(len(lines), 3)

    def test_selfcheck_reports_dead_broker(self):
        ok, lines = selfcheck("mqtt://127.0.0.1:1", connect_timeout=3)
        self.assertFalse(ok)
        self.assertIn("не удалось подключиться", lines[0])

    def test_failover_to_second_broker(self):
        code = generate_code()
        guest = Together()
        self.addCleanup(guest.stop)
        guest.configure(self.cfg("guest", code, "Боря", broker=f"mqtt://127.0.0.1:1, {self.broker}"))
        self.assertTrue(wait(lambda: guest.connection == "online", 25))


if __name__ == "__main__":
    unittest.main()
