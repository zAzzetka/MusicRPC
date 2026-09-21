"""«Слушать вместе»: один человек (хост) транслирует, что слушает, остальные (гости) повторяют.

Как это устроено
----------------
* Все участники подключаются к общему MQTT-брокеру (по умолчанию — публичные HiveMQ и EMQX,
  своих серверов у проекта нет) и обмениваются короткими сообщениями в «комнате».
* Комната — это код вида ``ABCD-EFGH-JKLM``. Из него выводятся имя канала и ключ шифрования,
  поэтому посторонний, не знающий кода, не найдёт канал и не прочитает сообщения
  (на публичных брокерах трафик видят все, так что шифрование обязательно).
* Хост шлёт своё состояние (трек, позиция, играет/пауза) при изменениях и раз в несколько секунд.
  Гость сообщает хосту, что он в комнате, чтобы хост видел, сколько человек слушает.
* Гость может: показывать трек хоста в своём Discord, подстраивать свой плеер (пауза, перемотка),
  если у него играет тот же трек, и открывать трек хоста в браузере.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .media import Track
from .textmatch import same_track

log = logging.getLogger("ymrpc.together")

# --- Настройки протокола -----------------------------------------------------------
DEFAULT_BROKERS = ("mqtt://broker.hivemq.com:1883", "mqtt://broker.emqx.io:1883")
TOPIC_ROOT = "musicrpc/v1/"

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 символа без похожих (нет I, O, 0, 1)
CODE_LEN = 12                                   # 12 символов по 5 бит = 60 бит

HEARTBEAT = 10.0        # как часто хост подтверждает состояние, сек
HELLO_EVERY = 20.0      # как часто гость сообщает хосту, что он в комнате, сек
STALE_AFTER = 35.0      # хост считается пропавшим, если от него тишина дольше, сек
GUEST_TTL = 60.0        # гость считается ушедшим, если от него тишина дольше, сек
PUBLISH_MIN_GAP = 1.0   # не чаще одного сообщения в секунду
SEEK_DRIFT = 3.0        # перемотку хоста замечаем, если позиция «уехала» больше чем на это, сек
MAX_CLOCK_SKEW = 600.0  # сообщения с временем, отличающимся от местного больше чем на это, отбрасываются
RETAINED_MAX_AGE = 45.0

# --- Синхронизация плеера гостя ----------------------------------------------------
SYNC_DRIFT = 3.0        # расхождение позиций, при котором делаем перемотку, сек
SYNC_COOLDOWN = 4.0     # не чаще одной команды плееру в N секунд
SEEK_COOLDOWN = 8.0     # не чаще одной перемотки в N секунд


# ================================================================================
# Код комнаты и шифрование
# ================================================================================
def format_code(raw: str) -> str:
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def generate_code() -> str:
    return format_code("".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN)))


def normalize_code(text) -> str | None:
    """Приводит введённый код к виду ABCD-EFGH-JKLM. None — если код некорректный."""
    raw = "".join(ch for ch in str(text or "").upper() if ch.isalnum())
    if len(raw) != CODE_LEN or any(ch not in ALPHABET for ch in raw):
        return None
    return format_code(raw)


@dataclass
class RoomKeys:
    host_topic: str
    guest_topic: str
    box: "Box"


def derive_room(code: str) -> RoomKeys:
    """Из кода комнаты получает имена каналов и ключи. Один и тот же код → одни и те же значения."""
    normalized = normalize_code(code)
    if normalized is None:
        raise ValueError("некорректный код комнаты")
    raw = normalized.replace("-", "")
    master = hashlib.pbkdf2_hmac("sha256", raw.encode("ascii"), b"musicrpc-together-v1", 100_000, 32)

    def sub(label: bytes) -> bytes:
        return hmac.new(master, label, hashlib.sha256).digest()

    base = TOPIC_ROOT + sub(b"topic").hex()[:32]
    return RoomKeys(base + "/host", base + "/guest", Box(sub(b"enc"), sub(b"mac")))


class Box:
    """Шифрование с проверкой подлинности (encrypt-then-MAC на HMAC-SHA256).

    Поток шифрования строится из HMAC-SHA256 в режиме счётчика, подпись — HMAC-SHA256 от
    «nonce + шифртекст». Схема стандартная и достаточна, чтобы случайные наблюдатели публичного
    брокера не прочитали и не подделали сообщения. Ключи выводятся из кода комнаты.
    """

    VERSION = b"\x01"

    def __init__(self, enc_key: bytes, mac_key: bytes):
        self._enc = enc_key
        self._mac = mac_key

    def _keystream_xor(self, data: bytes, nonce: bytes) -> bytes:
        out = bytearray(len(data))
        for block, pos in enumerate(range(0, len(data), 32)):
            pad = hmac.new(self._enc, nonce + block.to_bytes(4, "big"), hashlib.sha256).digest()
            for i, byte in enumerate(data[pos:pos + 32]):
                out[pos + i] = byte ^ pad[i]
        return bytes(out)

    def seal(self, obj: dict) -> bytes:
        plain = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(12)
        cipher = self._keystream_xor(plain, nonce)
        tag = hmac.new(self._mac, self.VERSION + nonce + cipher, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(self.VERSION + nonce + tag + cipher)

    def open(self, payload: bytes):
        """Расшифровывает сообщение. None — если оно повреждено, подделано или от другой комнаты."""
        try:
            raw = base64.urlsafe_b64decode(payload)
            if len(raw) < 1 + 12 + 16 or raw[:1] != self.VERSION:
                return None
            nonce, tag, cipher = raw[1:13], raw[13:29], raw[29:]
            good = hmac.new(self._mac, self.VERSION + nonce + cipher, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(tag, good):
                return None
            obj = json.loads(self._keystream_xor(cipher, nonce).decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None


# ================================================================================
# Состояние хоста, как его видит гость
# ================================================================================
@dataclass
class RemoteState:
    host_id: str
    host_name: str
    active: bool          # у хоста есть трек, которым он делится
    playing: bool
    title: str
    artist: str
    album: str
    position: float       # секунд — на момент received
    duration: float
    cover: str
    link: str
    received: float       # местное время получения
    seq: int = 0

    def position_now(self, now: float) -> float:
        pos = self.position + (max(now - self.received, 0.0) if self.playing else 0.0)
        if self.duration > 0:
            pos = min(pos, self.duration)
        return max(pos, 0.0)

    @property
    def key(self) -> tuple:
        return (self.host_id, self.title, self.artist)

    def as_track(self, now: float) -> Track:
        return Track("together", self.title, self.artist, self.album, self.playing,
                     self.position_now(now), self.duration)


def _clip(value, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


LINK_HOSTS = ("yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.uz")  # ссылки на треки ведут только сюда


def _safe_url(value, limit: int = 300, hosts=None) -> str:
    """Из сети приходит произвольный текст: ссылками считаем только http(s) (и, если задано, только с нужных сайтов)."""
    text = str(value or "").strip()
    if not text.lower().startswith(("http://", "https://")) or len(text) > limit:
        return ""
    if hosts:
        host = (urlparse(text).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in hosts):
            return ""
    return text


def _num(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number and abs(number) < 1e12 else default  # без NaN, inf и мусора
    except (TypeError, ValueError):
        return default


# ================================================================================
# Транспорт: MQTT
# ================================================================================
def parse_broker(text: str):
    """'mqtt://host:1883', 'mqtts://host:8883' или просто 'host[:port]' → (host, port, tls)."""
    text = text.strip()
    if "://" not in text:
        text = "mqtt://" + text
    url = urlparse(text)
    tls = url.scheme in ("mqtts", "ssl", "tls")
    if url.scheme not in ("mqtt", "mqtts", "tcp", "ssl", "tls"):
        raise ValueError(f"неподдерживаемый адрес брокера: {text}")
    if not url.hostname:
        raise ValueError(f"в адресе брокера нет хоста: {text}")
    return url.hostname, url.port or (8883 if tls else 1883), tls


def broker_list(setting: str) -> list:
    """Пустая настройка — брокеры по умолчанию; иначе один или несколько адресов через запятую."""
    items = [p.strip() for p in (setting or "").split(",") if p.strip() and p.strip().lower() != "auto"]
    return items or list(DEFAULT_BROKERS)


class MqttLink:
    """Соединение с брокером: подписка на канал, публикация, автоматическое переподключение и переход на запасной брокер."""

    def __init__(self, brokers, subscribe_topic, will_topic, on_message, on_status):
        self.brokers = list(brokers)
        self.subscribe_topic = subscribe_topic
        self.will_topic = will_topic
        self._on_message = on_message
        self._on_status = on_status
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._connected = threading.Event()
        self._client = None
        self._thread = None

    # -- управление ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="together-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._lost.set()
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> bool:
        client = self._client
        if client is None or not self._connected.is_set():
            return False
        try:
            client.publish(topic, payload, qos=0, retain=retain)
            return True
        except Exception as e:
            log.debug("Не удалось отправить сообщение: %s", e)
            return False

    # -- внутреннее ----------------------------------------------------------
    def _make_client(self, tls: bool):
        import paho.mqtt.client as mqtt

        client_id = "musicrpc-" + secrets.token_hex(6)
        v2 = hasattr(mqtt, "CallbackAPIVersion")
        if v2:  # paho-mqtt 2.x
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        else:   # paho-mqtt 1.x
            client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

        def on_connect(cl, userdata, *args):
            rc = args[1]  # у paho 1.x и 2.x код возврата стоит на одной и той же позиции
            failed = getattr(rc, "is_failure", None)
            failed = bool(failed) if failed is not None else rc != 0
            if failed:
                log.info("Брокер отклонил подключение: %s", rc)
                self._lost.set()
                return
            cl.subscribe(self.subscribe_topic, qos=0)
            self._connected.set()

        def on_disconnect(cl, userdata, *args):
            self._connected.clear()
            self._lost.set()

        def on_message(cl, userdata, msg):
            try:
                self._on_message(msg.topic, bytes(msg.payload), bool(msg.retain))
            except Exception:
                log.exception("Ошибка при обработке сообщения")

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        if self.will_topic:  # если хост пропал без предупреждения, брокер сам сообщит об этом гостям
            client.will_set(self.will_topic, payload=b"", qos=0, retain=True)
        if tls:
            client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=15)
        return client

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _run(self) -> None:
        try:
            import paho.mqtt.client  # noqa: F401
        except Exception as e:  # ImportError — пакет не установлен (например, после обновления без install.bat)
            log.error("Слушать вместе: пакет paho-mqtt недоступен: %s", e)
            self._on_status("missing", "paho-mqtt")
            return
        index = 0
        failures = 0
        while not self._stop.is_set():
            target = self.brokers[index % len(self.brokers)]
            client = None
            reached = False
            try:
                host, port, tls = parse_broker(target)
                self._on_status("connecting", f"{host}:{port}")
                self._connected.clear()
                self._lost.clear()
                client = self._make_client(tls)
                self._client = client
                client.connect(host, port, keepalive=30)
                client.loop_start()
                if self._connected.wait(15) and not self._lost.is_set():
                    reached = True
                    failures = 0
                    log.info("Слушать вместе: подключились к %s:%s", host, port)
                    self._on_status("online", f"{host}:{port}")
                    while not self._stop.is_set() and not self._lost.wait(1.0):
                        pass
                    if not self._stop.is_set():
                        log.info("Слушать вместе: связь с брокером потеряна")
                else:
                    log.info("Слушать вместе: брокер %s:%s не ответил вовремя", host, port)
            except Exception as e:
                log.info("Слушать вместе: не удалось подключиться к %s: %s", target, e)
            finally:
                self._connected.clear()
                self._client = None
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                    try:
                        client.loop_stop()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            if reached:
                self._on_status("connecting", "переподключение")
                self._sleep(1.0)        # связь была — пробуем тот же брокер
            else:
                failures += 1
                index += 1              # не вышло — следующий брокер из списка
                self._on_status("error", "нет связи с брокером")
                self._sleep(min(30.0, 2.0 ** min(failures, 5)))


# ================================================================================
# Сессия «слушать вместе»
# ================================================================================
DEFAULT_NAMES = {"host": "Хост", "guest": "Гость"}


class Together:
    """Хост публикует, гость читает. Все методы потокобезопасны."""

    def __init__(self, on_change=None, link_factory=None, clock=time.time):
        self._listeners = [on_change] if on_change else []
        self._link_factory = link_factory or MqttLink
        self._clock = clock
        self._lock = threading.RLock()

        self._settings = None
        self._mode = "off"
        self._code = ""
        self._name = ""
        self._room = None
        self._link = None
        self._sid = secrets.token_hex(4)   # случайный id участника на время работы программы
        self._gen = 0                      # номер соединения: сообщения от устаревших соединений игнорируются
        self._conn = "off"                 # off | connecting | online | error
        self._detail = ""
        self._reset()

    def _reset(self) -> None:
        self._seq = 0
        self._force = True
        self._last_sig = None
        self._last_start = None
        self._last_pub = 0.0
        self._last_hello = 0.0
        self._remote = None
        self._guests = {}

    def add_listener(self, fn) -> None:
        """fn() вызывается при любом изменении: подключение, новый гость, новое состояние хоста."""
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("Ошибка в обработчике изменений")

    # -- настройка -------------------------------------------------------------
    def configure(self, cfg) -> None:
        mode = cfg.together_mode
        code = normalize_code(cfg.together_room)
        if mode not in ("host", "guest") or code is None:
            mode = "off"
        brokers = tuple(broker_list(cfg.together_broker))
        settings = (mode, code, brokers)
        with self._lock:
            self._name = cfg.together_name.strip() or DEFAULT_NAMES.get(mode, "")
            if settings == self._settings:
                return
            old = (self._link, self._mode, self._room)
            self._gen += 1
            gen = self._gen
            self._link = self._room = None
            self._settings = settings
            self._mode = mode
            self._code = code or ""
            self._reset()
            self._conn = "off"
        self._close(*old)
        if mode == "off":
            self._changed()
            return
        try:
            room = derive_room(code)
        except Exception:
            log.exception("Не удалось подготовить комнату")
            return
        will = room.host_topic if mode == "host" else None
        subscribe = room.guest_topic if mode == "host" else room.host_topic
        link = self._link_factory(
            list(brokers), subscribe, will,
            lambda t, p, r, g=gen: self._on_message(g, t, p, r),
            lambda st, d="", g=gen: self._on_status(g, st, d),
        )
        with self._lock:
            if gen != self._gen:  # за это время настройки снова поменялись
                link = None
            else:
                self._room = room
                self._link = link
                self._conn = "connecting"
        if link is None:
            return
        log.info("Слушать вместе: режим %s, комната %s", mode, code)
        link.start()
        self._changed()

    @staticmethod
    def _close(link, mode, room) -> None:
        """Закрывает соединение. Хост перед этим сообщает гостям, что ушёл, и очищает сохранённое состояние."""
        if link is None:
            return
        try:
            if mode == "host" and room is not None:
                link.publish(room.host_topic, b"", retain=True)
                time.sleep(0.2)
        except Exception:
            pass
        try:
            link.stop()
        except Exception:
            log.exception("Ошибка при закрытии соединения")

    def stop(self) -> None:
        with self._lock:
            old = (self._link, self._mode, self._room)
            self._gen += 1
            self._link = self._room = None
            self._settings = None
            self._mode = "off"
            self._conn = "off"
        self._close(*old)

    # -- информация для интерфейса ---------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def code(self) -> str:
        return self._code

    @property
    def connection(self) -> str:
        return self._conn

    @property
    def detail(self) -> str:
        """К какому серверу подключены (например, broker.hivemq.com:1883)."""
        return self._detail

    def guests(self, now: float | None = None) -> list:
        now = self._clock() if now is None else now
        with self._lock:
            return sorted(name for name, seen in self._guests.values() if now - seen < GUEST_TTL)

    def describe(self, now: float | None = None) -> str:
        now = self._clock() if now is None else now
        with self._lock:
            mode, conn, code = self._mode, self._conn, self._code
        if mode == "off":
            return "Слушать вместе: выключено"
        if conn == "missing":
            return "Слушать вместе: не установлен paho-mqtt — запустите install.bat"
        if conn == "error":
            return "Слушать вместе: нет связи с сервером"
        if conn != "online":
            return "Слушать вместе: подключаюсь…"
        if mode == "host":
            names = self.guests(now)
            who = f" ({', '.join(names[:4])}{'…' if len(names) > 4 else ''})" if names else ""
            return f"Вы транслируете (комната {code}), слушают: {len(names)}{who}"
        remote = self.remote(now)
        if remote is None:
            return "Слушать вместе: жду хоста…"
        return f"Слушаете вместе с {remote.host_name}"

    # -- события транспорта ----------------------------------------------------
    def _on_status(self, gen: int, state: str, detail: str = "") -> None:
        with self._lock:
            if gen != self._gen or self._link is None:
                return
            self._conn = state
            self._detail = detail
            if state == "online":
                self._force = True
                self._last_hello = 0.0
        self._changed()

    def _on_message(self, gen: int, topic: str, payload: bytes, retained: bool) -> None:
        now = self._clock()
        with self._lock:
            if gen != self._gen:
                return
            mode, room = self._mode, self._room
        if room is None:
            return
        if not payload:  # пустое сообщение: хост вышел (или брокер объявил его «завещание»)
            if mode == "guest":
                with self._lock:
                    self._remote = None
                self._changed()
            return
        obj = room.box.open(payload)
        if obj is None:
            log.debug("Сообщение не удалось расшифровать — другая комната или подделка")
            return
        sent = _num(obj.get("ts"), 0.0)
        if abs(now - sent) > MAX_CLOCK_SKEW:
            log.warning("Сообщение отброшено: часы участников расходятся больше чем на %d мин", MAX_CLOCK_SKEW // 60)
            return
        kind = obj.get("k")
        peer = _clip(obj.get("id"), 16)
        if not peer or peer == self._sid:
            return
        if mode == "guest" and kind == "state":
            if retained and now - sent > RETAINED_MAX_AGE:
                return  # старое сохранённое состояние — дождёмся живого
            self._on_state(obj, peer, now)
        elif mode == "guest" and kind == "bye":
            with self._lock:
                if self._remote is not None and self._remote.host_id == peer:
                    self._remote = None
            self._changed()
        elif mode == "host" and kind == "hello":
            with self._lock:
                self._guests[peer] = (_clip(obj.get("n"), 32) or "Гость", now)
            self._changed()

    def _on_state(self, obj: dict, peer: str, now: float) -> None:
        seq = int(_num(obj.get("seq"), 0))
        with self._lock:
            cur = self._remote
            if cur is not None and cur.host_id != peer and now - cur.received < STALE_AFTER:
                return  # в комнате уже есть хост; второго игнорируем
            if cur is not None and cur.host_id == peer and seq and seq < cur.seq:
                return  # запоздавшее старое сообщение
            self._remote = RemoteState(
                host_id=peer,
                host_name=_clip(obj.get("n"), 32) or "Хост",
                active=bool(obj.get("a")),
                playing=bool(obj.get("p")),
                title=_clip(obj.get("title"), 200),
                artist=_clip(obj.get("artist"), 200),
                album=_clip(obj.get("album"), 200),
                position=max(_num(obj.get("pos")), 0.0),
                duration=max(_num(obj.get("dur")), 0.0),
                cover=_safe_url(obj.get("cover")),
                link=_safe_url(obj.get("link"), hosts=LINK_HOSTS),
                received=now,
                seq=seq,
            )
        self._changed()

    # -- отправка ----------------------------------------------------------------
    def _send(self, topic: str, obj: dict, retain: bool) -> bool:
        with self._lock:
            link, room = self._link, self._room
        if link is None or room is None:
            return False
        try:
            return link.publish(topic, room.box.seal(obj), retain)
        except Exception:
            log.exception("Не удалось отправить сообщение")
            return False

    def _base(self, kind: str, now: float) -> dict:
        return {"v": 1, "k": kind, "id": self._sid, "n": self._name, "ts": int(now)}

    def tick(self, now: float) -> None:
        """Раз в цикл: гость даёт знать о себе."""
        with self._lock:
            if self._mode != "guest" or self._conn != "online" or self._room is None:
                return
            if now - self._last_hello < HELLO_EVERY:
                return
            topic = self._room.guest_topic
        if self._send(topic, self._base("hello", now), retain=False):
            self._last_hello = now

    def publish_local(self, track: Track | None, cover: str | None, link: str | None, now: float) -> None:
        """Хост: сообщает, что играет (track=None — нечего показывать)."""
        with self._lock:
            if self._mode != "host" or self._conn != "online" or self._room is None:
                return
            topic = self._room.host_topic
            force = self._force
        active = track is not None
        playing = bool(track and track.playing)
        sig = (active, playing, track.title if track else "", track.artist if track else "",
               track.album if track else "", cover or "", link or "")
        start = now - track.position if (track is not None and playing) else None

        need = force or sig != self._last_sig
        if not need and start is not None and self._last_start is not None \
                and abs(start - self._last_start) >= SEEK_DRIFT:
            need = True  # перемотали
        if not need and now - self._last_pub >= HEARTBEAT:
            need = True
        if not need:
            return
        if not force and now - self._last_pub < PUBLISH_MIN_GAP:
            return

        with self._lock:
            self._seq += 1
            seq = self._seq
        msg = self._base("state", now)
        msg.update({
            "seq": seq, "a": int(active), "p": int(playing),
            "title": _clip(track.title if track else "", 200),
            "artist": _clip(track.artist if track else "", 200),
            "album": _clip(track.album if track else "", 200),
            "pos": round(track.position, 1) if track else 0,
            "dur": round(track.duration, 1) if track else 0,
            "cover": _clip(cover, 300), "link": _clip(link, 300),
        })
        if self._send(topic, msg, retain=True):
            with self._lock:
                self._force = False
                self._last_sig = sig
                self._last_start = start
                self._last_pub = now

    def remote(self, now: float | None = None) -> RemoteState | None:
        """Гость: что сейчас слушает хост. None — хоста нет, он пропал или ему нечем делиться."""
        now = self._clock() if now is None else now
        with self._lock:
            r = self._remote
            if self._mode != "guest" or r is None or not r.active:
                return None
            if now - r.received > STALE_AFTER:
                return None
            return r


# ================================================================================
# Синхронизация плеера гостя с хостом
# ================================================================================
@dataclass
class SyncState:
    last_cmd: float = 0.0
    last_seek: float = 0.0
    seek_ok: bool = True      # плеер принимает перемотку? (после двух неудач до смены трека — нет)
    seek_failures: int = 0
    opened_key: tuple = field(default_factory=tuple)
    track_key: tuple = field(default_factory=tuple)
    matched_key: tuple = field(default_factory=tuple)   # для какого трека хоста уже выровняли пауза/плей
    remote_playing: bool | None = None


def plan_sync(local: Track | None, remote: RemoteState | None, now: float, st: SyncState,
              sync_player: bool, auto_open: bool) -> list:
    """Что нужно сделать гостю, чтобы слушать вместе с хостом.

    Возвращает список действий: ("play",), ("pause",), ("seek", секунды), ("open", ссылка).
    Плеером управляем только если у гостя играет ТОТ ЖЕ трек, что у хоста.
    """
    if remote is None or not remote.active:
        return []
    if local is not None and local.key != st.track_key:  # сменился трек у гостя — перемотка снова разрешена
        st.track_key, st.seek_ok, st.seek_failures = local.key, True, 0

    same = local is not None and same_track(local.title, local.artist, remote.title, remote.artist)
    if same and sync_player:
        if now - st.last_cmd < SYNC_COOLDOWN:
            return []
        actions: list = []
        # Пауза/воспроизведение — по событиям: когда трек совпал впервые или хост нажал паузу/плей.
        # Если гость потом сам поставил паузу, мы её не отменяем, пока хост что-то не переключит.
        if st.matched_key != remote.key or st.remote_playing != remote.playing:
            if remote.playing and not local.playing:
                actions.append(("play",))
            elif not remote.playing and local.playing:
                actions.append(("pause",))
            st.matched_key, st.remote_playing = remote.key, remote.playing
        if not actions and remote.playing and local.playing and st.seek_ok \
                and now - st.last_seek >= SEEK_COOLDOWN:
            target = remote.position_now(now)
            if abs(local.position - target) > SYNC_DRIFT:
                actions.append(("seek", target + 0.3))  # +0.3 с — поправка на время выполнения команды
        return actions
    if not same and auto_open and remote.link and remote.playing and st.opened_key != remote.key:
        return [("open", remote.link)]
    return []


def selfcheck(broker_setting: str = "", link_factory=None, connect_timeout: float = 20.0,
              receive_timeout: float = 10.0) -> tuple:
    """Проверка «слушать вместе» одним компьютером.

    Поднимает в этой программе хоста и гостя в новой случайной комнате и передаёт тестовый трек через настоящий сервер.
    Так проверяются сразу: установлен ли paho-mqtt, доступен ли сервер, работает ли шифрование и получает ли гость,
    зашедший позже, сохранённое состояние. Возвращает (всё ли в порядке, строки отчёта).
    """
    from types import SimpleNamespace

    lines: list = []
    if link_factory is None:
        try:
            import paho.mqtt.client  # noqa: F401
        except Exception as e:
            return False, [f"✗ не установлен пакет paho-mqtt ({e}). Запустите install.bat"]

    code = generate_code()

    def cfg(mode: str, name: str):
        return SimpleNamespace(together_mode=mode, together_room=code, together_name=name,
                               together_broker=broker_setting)

    def wait_for(condition, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.05)
        return condition()

    peers = []
    try:
        host = Together(link_factory=link_factory)
        guest = Together(link_factory=link_factory)
        peers += [host, guest]
        started = time.time()
        host.configure(cfg("host", "Проверка (хост)"))
        guest.configure(cfg("guest", "Проверка (гость)"))
        if not wait_for(lambda: host.connection == "online" and guest.connection == "online", connect_timeout):
            lines.append(f"✗ не удалось подключиться к серверу за {connect_timeout:.0f} с")
            if broker_setting.strip():
                lines.append("  Проверьте адрес своего сервера, что он запущен и что порт открыт (mqtt://адрес:1883).")
            else:
                lines.append("  Публичные серверы могут быть заблокированы провайдером или файрволом (порт 1883).")
                lines.append("  Попробуйте VPN или укажите свой сервер: mqtt://адрес:1883 или mqtts://адрес:8883.")
            return False, lines
        lines.append(f"✓ подключение к серверу {host.detail} ({time.time() - started:.1f} с)")

        sent = time.time()
        marker = Track("check", "Проверка связи", "Music RPC", "", True, 30.0, 120.0)
        host.publish_local(marker, None, None, sent)
        if not wait_for(lambda: getattr(guest.remote(time.time()), "title", None) == marker.title, receive_timeout):
            lines.append(f"✗ гость не получил сообщение хоста за {receive_timeout:.0f} с")
            lines.append("  Сервер принял подключение, но сообщения не доходят. Попробуйте позже или другой сервер.")
            return False, lines
        lines.append(f"✓ хост → гость: сообщение зашифровано, расшифровано и получено за {time.time() - sent:.1f} с")

        late = Together(link_factory=link_factory)
        peers.append(late)
        late.configure(cfg("guest", "Проверка (опоздавший)"))
        if wait_for(lambda: late.remote(time.time()) is not None, receive_timeout):
            lines.append("✓ гость, зашедший позже, сразу получает текущий трек (сервер хранит последнее состояние)")
        else:
            lines.append("! опоздавший гость не получил сохранённое состояние: гости, зашедшие позже, увидят трек "
                         "только после следующего «пульса» хоста (до 10 с). Это не мешает работе")
        return True, lines
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка самопроверки")
        lines.append(f"✗ ошибка проверки: {type(e).__name__}: {e}")
        return False, lines
    finally:
        for peer in peers:
            try:
                peer.stop()
            except Exception:  # noqa: BLE001
                pass


def open_url(url: str) -> bool:
    """Открывает ссылку в браузере по умолчанию."""
    if not url.lower().startswith(("http://", "https://")):
        return False  # из сети приходит только текст: открываем лишь обычные ссылки
    try:
        if hasattr(os, "startfile"):
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(url)
        return True
    except Exception as e:
        log.warning("Не удалось открыть ссылку: %s", e)
        return False
