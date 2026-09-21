"""Окно настроек и меню в трее. Нужны дисплей (на Linux — Xvfb), tkinter и pystray; иначе пропускается."""
import concurrent.futures
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.helpers  # noqa: F401  (добавляет корень проекта в sys.path)
from ymrpc.config import ConfigStore
from ymrpc.media import Track
from ymrpc.status import Status
from ymrpc.together import Together

try:
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        raise ImportError("нет дисплея")
    os.environ.setdefault("PYSTRAY_BACKEND", "xorg")
    import tkinter as tk
    from ymrpc import tray as tray_mod
    from ymrpc.ui import SettingsWindow, STATUS_LABELS
    HAVE_UI = True
except Exception as e:  # noqa: BLE001
    HAVE_UI = False
    WHY = str(e)


class FakeWorker:
    def __init__(self, store):
        from ymrpc.history import History
        self.woke = False
        self.together = Together()
        self.history = History()
        self._link = None

    def wake(self):
        self.woke = True

    def list_sources(self):
        f = concurrent.futures.Future()
        f.set_result([Track("ru.yandex.music", "Song", "Artist", "", True, 0, 0)])
        return f

    def current(self):
        return (None, self._link, None)


@unittest.skipUnless(HAVE_UI, "нужны дисплей, tkinter и pystray")
class UiTests(unittest.TestCase):
    def setUp(self):
        self.store = ConfigStore(Path(tempfile.mkdtemp()) / "c.json")
        self.worker = FakeWorker(self.store)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_settings_roundtrip_keeps_unknown_fields(self):
        self.store.update(together_broker="mqtt://x:1", hide_keywords=["a"])
        sw = SettingsWindow(self.root, self.store, self.worker)
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch("tkinter.messagebox.showwarning") as warn, \
                mock.patch("ymrpc.autostart.set_enabled"):
            sw.show()
            self.root.update()
            sw.v_client.set("123456789012345678")
            sw.v_filters.set("yandex, spotify")
            sw.v_details.set("{artist} — {title}")
            sw.v_status.set(STATUS_LABELS["state"])
            sw.v_interval.set("3")
            sw.v_hide.set("secret, demo")
            sw._check_sources()
            self.root.update()
            import time
            time.sleep(0.4)
            self.root.update()
            self.assertTrue(info.called)
            sw._save()
            self.root.update()
            c = self.store.get()
            self.assertEqual((c.client_id, c.app_filters, c.details_format, c.status_display, c.poll_interval),
                             ("123456789012345678", ["yandex", "spotify"], "{artist} — {title}", "state", 3.0))
            self.assertEqual(c.hide_keywords, ["secret", "demo"])
            self.assertEqual(c.together_broker, "mqtt://x:1")   # поля, которых нет в окне, не сбрасываются
            self.assertTrue(self.worker.woke)

            sw.show()
            self.root.update()
            sw.v_client.set("abc")
            sw._save()
            self.assertTrue(warn.called and sw.win.winfo_exists())
            win = sw.win
            sw.show()
            self.assertIs(sw.win, win)
            sw.win.destroy()

    def test_together_tab_validation_and_host_autocode(self):
        sw = SettingsWindow(self.root, self.store, self.worker)
        with mock.patch("tkinter.messagebox.showinfo"), mock.patch("tkinter.messagebox.showwarning") as warn, \
                mock.patch("ymrpc.autostart.set_enabled"):
            sw.show(tab="together")
            self.root.update()
            self.assertEqual(sw._tabs.index(sw._tabs.select()), 2)
            sw.v_tmode.set("guest")
            sw.v_room.set("wrong")
            sw._save()
            self.assertTrue(warn.called)                        # гостю нужен корректный код
            self.assertEqual(self.store.get().together_mode, "off")
            sw.v_tmode.set("host")
            sw.v_room.set("")
            sw._save()
            c = self.store.get()
            self.assertEqual(c.together_mode, "host")
            self.assertRegex(c.together_room, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")   # код создан автоматически

    def test_check_connection_button(self):
        sw = SettingsWindow(self.root, self.store, self.worker)
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch("tkinter.messagebox.showwarning") as warn, \
                mock.patch("ymrpc.ui.selfcheck") as check, mock.patch("ymrpc.autostart.set_enabled"):
            sw.show(tab="together")
            self.root.update()
            check.return_value = (True, ["✓ подключение к серверу test:1883 (0.1 с)"])
            sw.v_broker.set("mqtt://test:1883")
            sw._check_together()
            self.assertIn("disabled", sw.btn_check.state())          # пока идёт проверка, кнопка неактивна
            import time
            end = time.time() + 5
            while not info.called and time.time() < end:
                self.root.update()
                time.sleep(0.05)
            check.assert_called_once_with("mqtt://test:1883")
            self.assertTrue(info.called and not warn.called)
            self.assertNotIn("disabled", sw.btn_check.state())

            info.reset_mock()
            check.return_value = (False, ["✗ не удалось подключиться к серверу за 20 с"])
            sw._check_together()
            end = time.time() + 5
            while not warn.called and time.time() < end:
                self.root.update()
                time.sleep(0.05)
            self.assertTrue(warn.called)
            sw.win.destroy()

    def test_tray_menu(self):
        tray_mod.TITLE = "MusicRPC"   # X11-бэкенд не принимает кириллицу в заголовке; на Windows это не нужно
        q = queue.Queue()
        st = Status()
        t = tray_mod.Tray(self.store, st, self.worker, q)
        top = list(t.icon.menu.items)
        texts = [i.text if isinstance(i.text, str) else "<динамический>" for i in top]
        for needed in ("Треки", "Слушать вместе", "Настройки…", "Выход"):
            self.assertIn(needed, texts)

        tracks = next(i for i in top if i.text == "Треки")
        self.assertEqual(len(list(tracks.submenu.items)), 4)     # 2 действия, разделитель, «Пока ничего не играло»
        self.worker.history.add("AC/DC & Co", "Song", "https://music.yandex.ru/track/1")
        items = list(tracks.submenu.items)
        self.assertEqual(items[-1].text, "AC/DC && Co — Song")   # «&» экранируется для меню Windows
        with mock.patch("ymrpc.tray.open_url") as opener:
            items[-1](t.icon)
            opener.assert_called_once_with("https://music.yandex.ru/track/1")

        together = next(i for i in top if i.text == "Слушать вместе")
        t_items = list(together.submenu.items)
        host = next(i for i in t_items if i.text == "Я транслирую (хост)")
        with mock.patch.object(t, "notify"):
            host(t.icon)
        c = self.store.get()
        self.assertEqual(c.together_mode, "host")
        self.assertTrue(c.together_room)
        self.assertEqual(q.get_nowait(), ("copy", c.together_room))   # код отправлен в буфер обмена
        guest = next(i for i in t_items if i.text.startswith("Я слушаю друга"))
        guest(t.icon)
        self.assertEqual(q.get_nowait(), ("settings", "together"))
        off = next(i for i in t_items if i.text == "Выключено")
        off(t.icon)
        self.assertEqual(self.store.get().together_mode, "off")

        toggle = next(i for i in top if i.text == "Обложка")
        before = self.store.get().show_cover
        toggle(t.icon)
        self.assertNotEqual(self.store.get().show_cover, before)
        st.set("playing", Track("y", "Song", "Simon & Garfunkel", "", True, 0, 0))
        self.assertIn("Song", t.icon.title)
        status_item = top[0]
        self.assertIn("Simon && Garfunkel", status_item.text)     # «&» не превращается в горячую клавишу


if __name__ == "__main__":
    unittest.main()
