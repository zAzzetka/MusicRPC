"""Текущее состояние программы — его читают иконка в трее и меню."""
from __future__ import annotations

import threading

STATE_TEXT = {
    "starting": "Запуск…",
    "disabled": "Выключено",
    "no_client_id": "Укажите Client ID в настройках",
    "bad_client_id": "Неверный Client ID (см. настройки)",
    "waiting": "Жду, пока что-нибудь заиграет",
    "hidden": "Скрыто вашим фильтром",
    "paused": "На паузе",
    "playing": "Играет",
    "discord_missing": "Discord не найден — запустите его",
    "discord_error": "Discord не отвечает (подробности в app.log)",
    "error": "Ошибка (см. app.log)",
}

# Цвет иконки в трее для каждого состояния
STATE_COLOR = {
    "starting": (128, 128, 140),
    "disabled": (80, 80, 90),
    "waiting": (128, 128, 140),
    "hidden": (128, 128, 140),
    "paused": (108, 92, 168),
    "playing": (139, 92, 246),
    "no_client_id": (226, 75, 74),
    "bad_client_id": (226, 75, 74),
    "discord_missing": (226, 75, 74),
    "discord_error": (226, 75, 74),
    "error": (226, 75, 74),
}


class Status:
    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self.state = "starting"
        self.track = None
        self._listeners = []

    def subscribe(self, fn) -> None:
        """fn(status) вызывается при каждом изменении состояния или трека."""
        self._listeners.append(fn)

    def set(self, state: str, track=None) -> None:
        key = (state, track.key if track else None, track.playing if track else None)
        with self._lock:
            changed = key != self._key
            self._key = key
            self.state = state
            self.track = track
        if changed:
            for fn in list(self._listeners):
                try:
                    fn(self)
                except Exception:
                    pass

    def describe(self, limit: int = 60) -> str:
        text = STATE_TEXT.get(self.state, self.state)
        t = self.track
        if t is not None and self.state in ("playing", "paused"):
            name = f"{t.artist} — {t.title}" if t.artist else t.title
            text = f"{text}: {name}"
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text
