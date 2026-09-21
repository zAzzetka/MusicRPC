"""Иконка в системном трее и её меню."""
from __future__ import annotations

import logging
import os
import sys
import time

import pystray
from pystray import Menu
from pystray import MenuItem as Item

from . import APP_NAME, DISPLAY_NAME, autostart
from .config import ConfigStore, data_dir
from .icon import make_icon
from .status import STATE_COLOR, Status
from .together import generate_code, open_url

log = logging.getLogger("ymrpc.tray")

TITLE = DISPLAY_NAME


class Tray:
    def __init__(self, store: ConfigStore, status: Status, worker, ui_queue):
        self.store = store
        self.status = status
        self.worker = worker
        self.ui_queue = ui_queue
        self._icons = {state: make_icon(color, 64) for state, color in STATE_COLOR.items()}

        self.icon = pystray.Icon(
            APP_NAME,
            self._icons["starting"],
            TITLE,
            menu=self._build_menu(),
        )
        status.subscribe(self._on_status)

    # -- меню ---------------------------------------------------------------
    def _checked(self, key: str):
        return lambda item: bool(getattr(self.store.get(), key))

    def _toggle(self, key: str):
        def action(icon, item):
            self.store.update(**{key: not getattr(self.store.get(), key)})
            self.worker.wake()
            icon.update_menu()

        return action

    def _toggle_autostart(self, icon, item):
        new_value = not self.store.get().autostart
        self.store.update(autostart=new_value)
        autostart.set_enabled(new_value)
        icon.update_menu()

    # -- «Треки» -------------------------------------------------------------
    @staticmethod
    def _text(text: str, limit: int = 60) -> str:
        """Текст пункта меню: обрезаем и экранируем «&» (в меню Windows это признак горячей клавиши)."""
        text = text if len(text) <= limit else text[: limit - 1] + "…"
        return text.replace("&", "&&")

    def _current_link(self):
        return self.worker.current()[1]

    def _copy(self, text: str) -> None:
        if text:
            self.ui_queue.put(("copy", text))

    def _recent_action(self, item):
        def action(icon, _item):
            if item.link:
                open_url(item.link)
            else:
                self._copy(item.label)  # ссылки нет — хотя бы копируем название

        return action

    def _tracks_menu(self):
        items = [
            Item("Открыть текущий трек", lambda icon, item: open_url(self._current_link() or ""),
                 enabled=lambda item: bool(self._current_link())),
            Item("Скопировать ссылку на текущий трек", lambda icon, item: self._copy(self._current_link() or ""),
                 enabled=lambda item: bool(self._current_link())),
            Menu.SEPARATOR,
        ]
        recent = self.worker.history.items()
        if not recent:
            items.append(Item("Пока ничего не играло", None, enabled=False))
        for entry in recent:
            items.append(Item(self._text(entry.label), self._recent_action(entry)))
        return tuple(items)

    # -- «Слушать вместе» ----------------------------------------------------
    def _mode_checked(self, mode: str):
        return lambda item: self.store.get().together_mode == mode

    def _set_mode(self, mode: str):
        def action(icon, item):
            cfg = self.store.get()
            if mode == "guest":
                # нужен код комнаты друга — просим ввести его в настройках
                self.ui_queue.put(("settings", "together"))
                return
            if mode == "host":
                # своя комната: если раньше были гостем, чужой код брать нельзя
                room = cfg.together_room if cfg.together_mode != "guest" and cfg.together_room else generate_code()
                self.store.update(together_mode="host", together_room=room)
                self._copy(room)
                self.notify(f"Вы транслируете. Код комнаты скопирован: {room}. Отправьте его друзьям.")
            else:
                self.store.update(together_mode="off")
            self.worker.wake()
            icon.update_menu()

        return action

    def _copy_room(self, icon, item):
        room = self.store.get().together_room
        self._copy(room)
        if room:
            self.notify(f"Код комнаты скопирован: {room}")

    def _host_link(self):
        remote = self.worker.together.remote(time.time())
        return remote.link if remote is not None else ""

    def _together_menu(self):
        return (
            Item(lambda item: self._text(self.worker.together.describe(), 100), None, enabled=False),
            Menu.SEPARATOR,
            Item("Выключено", self._set_mode("off"), checked=self._mode_checked("off"), radio=True),
            Item("Я транслирую (хост)", self._set_mode("host"), checked=self._mode_checked("host"), radio=True),
            Item("Я слушаю друга (гость)…", self._set_mode("guest"), checked=self._mode_checked("guest"), radio=True),
            Menu.SEPARATOR,
            Item("Скопировать код комнаты", self._copy_room, enabled=lambda item: bool(self.store.get().together_room)),
            Item("Открыть трек хоста", lambda icon, item: open_url(self._host_link()),
                 enabled=lambda item: bool(self._host_link())),
            Item("Настройки «слушать вместе»…", lambda icon, item: self.ui_queue.put(("settings", "together"))),
        )

    def _build_menu(self) -> Menu:
        return Menu(
            Item(lambda item: self._text(self.status.describe(), 100), None, enabled=False),
            Item(lambda item: self._text(self.worker.together.describe(), 100), None, enabled=False,
                 visible=lambda item: self.store.get().together_mode != "off"),
            Menu.SEPARATOR,
            Item("Показывать статус в Discord", self._toggle("enabled"), checked=self._checked("enabled")),
            Item("Обложка", self._toggle("show_cover"), checked=self._checked("show_cover")),
            Item("Полоса прогресса", self._toggle("show_progress"), checked=self._checked("show_progress")),
            Item("Кнопка со ссылкой на трек", self._toggle("show_button"), checked=self._checked("show_button")),
            Item("Показывать на паузе", self._toggle("show_paused"), checked=self._checked("show_paused")),
            Menu.SEPARATOR,
            Item("Треки", Menu(self._tracks_menu)),
            Item("Слушать вместе", Menu(self._together_menu)),
            Menu.SEPARATOR,
            Item("Автозапуск с Windows", self._toggle_autostart, checked=self._checked("autostart")),
            Item("Настройки…", lambda icon, item: self.ui_queue.put(("settings", None)), default=True),
            Item("Открыть папку с логом", lambda icon, item: self._open_folder()),
            Menu.SEPARATOR,
            Item("Выход", lambda icon, item: self.ui_queue.put(("quit", None))),
        )

    @staticmethod
    def _open_folder() -> None:
        try:
            if sys.platform == "win32":
                os.startfile(data_dir())  # type: ignore[attr-defined]
        except Exception as e:
            log.warning("Не удалось открыть папку: %s", e)

    # -- состояние ----------------------------------------------------------
    def _on_status(self, status: Status) -> None:
        try:
            self.icon.icon = self._icons.get(status.state, self._icons["starting"])
            self.icon.title = f"{TITLE}\n{status.describe(100)}"[:127]
            self.icon.update_menu()
        except Exception:
            pass  # иконка ещё не запущена или уже закрыта

    def refresh(self) -> None:
        """Перерисовать меню (например, изменилось число слушателей)."""
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, message: str, title: str = TITLE) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    # -- жизненный цикл -----------------------------------------------------
    def run(self) -> None:
        self.icon.run()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
