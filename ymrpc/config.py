"""Настройки программы: хранятся в JSON в %APPDATA%\\MusicRPC\\config.json."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import APP_NAME, LEGACY_APP_NAMES

log = logging.getLogger("ymrpc.config")


def data_dir() -> Path:
    """Папка с конфигом и логом. Настройки из папок прежних версий подхватываются один раз."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    folder = root / APP_NAME
    fresh = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    if fresh:
        for legacy in LEGACY_APP_NAMES:
            old_cfg = root / legacy / "config.json"
            if old_cfg.exists():
                try:
                    shutil.copy2(old_cfg, folder / "config.json")
                except OSError:
                    pass
                break
    return folder


def log_path() -> Path:
    return data_dir() / "app.log"


STATUS_DISPLAY_CHOICES = ("name", "details", "state")
TOGETHER_MODES = ("off", "host", "guest")


@dataclass
class Config:
    # --- Discord ---
    client_id: str = ""            # Application ID из discord.com/developers
    enabled: bool = True           # общий выключатель

    # --- Откуда брать музыку ---
    # Подстроки, которые ищутся в идентификаторе приложения-источника (SMTC).
    # Пустой список = брать любой источник, который играет.
    app_filters: list = field(default_factory=lambda: ["yandex", "яндекс"])

    # --- Что показывать ---
    show_cover: bool = True        # обложка
    show_progress: bool = True     # полоса прогресса / таймер
    show_button: bool = True       # кнопка со ссылкой на трек (другие видят, вы — нет)
    show_paused: bool = False      # показывать трек, когда он на паузе
    button_label: str = "Открыть трек"   # подпись кнопки (до 32 символов)

    # --- Тексты. Доступно: {title} {artist} {album} ---
    details_format: str = "{title}"
    state_format: str = "{artist}"
    large_text_format: str = "{album}"
    status_display: str = "name"   # что писать в списке участников: name | details | state

    # --- Скрывать ---
    # Если любое из слов встретится в названии, исполнителе, альбоме или названии плеера —
    # статус не показывается (и трек не попадает в «слушать вместе»).
    hide_keywords: list = field(default_factory=list)

    # --- Слушать вместе ---
    together_mode: str = "off"           # off — выключено, host — я транслирую, guest — я слушаю друга
    together_room: str = ""              # код комнаты вида ABCD-EFGH-JKLM
    together_name: str = ""              # как вас видят друзья
    together_broker: str = ""            # пусто — публичные брокеры по умолчанию; или mqtt://host:1883, mqtts://host:8883
    together_mirror: bool = True         # гость: показывать в своём Discord трек хоста
    together_sync_player: bool = True    # гость: подстраивать свой плеер (пауза/перемотка), если играет тот же трек
    together_auto_open: bool = False     # гость: открывать трек хоста в браузере, когда у вас играет другой
    together_suffix: str = "· вместе с {name}"   # добавка ко второй строке статуса; {name} — имя хоста

    # --- Прочее ---
    poll_interval: float = 2.0     # как часто опрашивать плеер, сек
    autostart: bool = False        # запуск вместе с Windows
    yandex_token: str = ""         # необязательно: OAuth-токен, если обложки и ссылки не находятся


_DEFAULTS = Config()


def _coerce(name: str, value):
    """Приводит значение из JSON к типу поля; при несовпадении возвращает None."""
    default = getattr(_DEFAULTS, name)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None
    if isinstance(default, str):
        return value if isinstance(value, str) else None
    if isinstance(default, list):
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return None
    return None


def sanitize(cfg: Config) -> Config:
    """Приводит значения к допустимым."""
    cfg.client_id = cfg.client_id.strip()
    cfg.poll_interval = min(max(float(cfg.poll_interval), 1.0), 10.0)
    if cfg.status_display not in STATUS_DISPLAY_CHOICES:
        cfg.status_display = "name"
    cfg.app_filters = [f.strip() for f in cfg.app_filters if f.strip()]
    cfg.button_label = cfg.button_label.strip()[:32] or "Открыть трек"
    cfg.hide_keywords = [k.strip() for k in cfg.hide_keywords if k.strip()]
    if cfg.together_mode not in TOGETHER_MODES:
        cfg.together_mode = "off"
    from .together import normalize_code  # импорт здесь: together не нужен, пока не включён

    cfg.together_room = normalize_code(cfg.together_room) or ""
    cfg.together_name = cfg.together_name.strip()[:32]
    cfg.together_broker = cfg.together_broker.strip()
    cfg.together_suffix = cfg.together_suffix.strip()[:64]
    return cfg


class ConfigStore:
    """Потокобезопасное хранилище настроек."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "config.json")
        self._lock = threading.RLock()
        self.first_run = not self.path.exists()
        self._cfg = self._load()
        if self.first_run:
            self.save()

    # -- чтение / запись ----------------------------------------------------
    def _load(self) -> Config:
        cfg = Config()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json должен быть объектом")
        except FileNotFoundError:
            return cfg
        except Exception as e:  # битый файл — работаем с настройками по умолчанию
            log.warning("Не удалось прочитать %s: %s", self.path, e)
            return cfg
        for f in fields(Config):
            if f.name in raw:
                value = _coerce(f.name, raw[f.name])
                if value is not None:
                    setattr(cfg, f.name, value)
        return sanitize(cfg)

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(asdict(self._cfg), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    # -- доступ -------------------------------------------------------------
    def get(self) -> Config:
        """Копия текущих настроек (можно спокойно читать из любого потока)."""
        with self._lock:
            return copy.deepcopy(self._cfg)

    def update(self, **changes) -> Config:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self._cfg, key):
                    raise AttributeError(key)
                setattr(self._cfg, key, value)
            sanitize(self._cfg)
            self.save()
            return copy.deepcopy(self._cfg)

    def replace(self, cfg: Config) -> Config:
        with self._lock:
            self._cfg = sanitize(copy.deepcopy(cfg))
            self.save()
            return copy.deepcopy(self._cfg)
