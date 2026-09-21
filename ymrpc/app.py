"""Точка входа: собирает всё вместе и крутит окно/трей/рабочий поток."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import queue
import sys
import threading

from . import APP_NAME, DISPLAY_NAME, __version__, autostart
from .config import ConfigStore, log_path

log = logging.getLogger("ymrpc")


def setup_logging(debug: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if sys.stderr is not None:  # в консоли (diagnose.bat) тоже показываем
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)

    def excepthook(exc_type, exc, tb):
        logging.getLogger("ymrpc").error("Необработанная ошибка", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    threading.excepthook = lambda a: excepthook(a.exc_type, a.exc_value, a.exc_traceback)


def acquire_single_instance():
    """Не даёт запустить программу дважды. Возвращает handle (держим до выхода) или None."""
    if sys.platform != "win32":
        return object()
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_singleton")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle or object()


def prepare_windows() -> None:
    """Чёткий текст на экранах с масштабированием и своя группа значков на панели задач."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:  # без этого окно tkinter сгруппируется на панели задач вместе с Python
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Диагностика: diagnose.bat
# ---------------------------------------------------------------------------
async def _diagnose_discord(client_id: str) -> None:
    from pypresence import ActivityType, InvalidID

    from .rpc import _default_rpc_factory, _hard_close, describe_error, open_presence

    if sys.platform == "win32":
        try:
            pipes = sorted(n for n in os.listdir(r"\\.\pipe") if n.startswith("discord-ipc"))
        except OSError:
            pipes = []
        print("Каналы Discord в системе:", ", ".join(pipes) if pipes else "не найдены")

    try:
        rpc, errors = await open_presence(_default_rpc_factory, client_id)
    except InvalidID:
        print("✗ Discord отклонил Client ID. Проверьте, что скопировали именно «ID приложения».")
        return
    for pipe, err in errors:
        print(f"  канал {pipe}: {describe_error(err)}")
    if rpc is None:
        print("✗ Подключиться к Discord не удалось.")
        print("  • Discord должен быть запущен как программа для компьютера (не во вкладке браузера).")
        print("  • Discord и эта программа должны быть запущены с одинаковыми правами "
              "(не «от имени администратора» только у одного из них).")
        print("  • Попробуйте полностью закрыть Discord (значок в трее → Выйти) и открыть заново.")
        return

    print("✓ Подключение к Discord есть.")
    try:
        await rpc.update(activity_type=ActivityType.LISTENING, details="Проверка связи", state=DISPLAY_NAME)
        print("  Тестовый статус отправлен — 6 секунд посмотрите в профиле Discord…")
        await asyncio.sleep(6)
        await rpc.clear()
    except Exception as e:
        print(f"✗ Не удалось отправить статус: {describe_error(e)}")
    finally:
        _hard_close(rpc)


def diagnose() -> int:
    """diagnose.bat: что видит медиа-центр Windows и работает ли связь с Discord."""
    from .media import MediaReader

    for stream in (sys.stdout, sys.stderr):  # консоль Windows в cp866/cp1251 не знает символов ✓ ✗ — не падаем
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    print("1. Что сейчас видит медиа-центр Windows (SMTC):\n")
    try:
        tracks = asyncio.run(MediaReader().list_sources())
    except Exception as e:
        print(f"Ошибка: {e}")
        tracks = []
    if not tracks:
        print("Ничего. Запустите плеер, включите трек и повторите.")
    for t in tracks:
        state = "играет" if t.playing else "пауза"
        print(f"Источник : {t.app_id}")
        print(f"Трек     : {t.artist} — {t.title}   [{state}]")
        print(f"Позиция  : {t.position:.0f} c из {t.duration:.0f} c\n")
    if tracks:
        print("Часть названия нужного источника впишите в настройках, поле «Приложения».")

    print("\n2. Связь с Discord:\n")
    cfg = ConfigStore().get()
    if not cfg.client_id:
        print("Client ID не указан — откройте настройки программы.")
    else:
        try:
            asyncio.run(_diagnose_discord(cfg.client_id))
        except Exception as e:
            print(f"Ошибка проверки: {e}")

    print("\n3. Серверы для «слушать вместе» (нужны только для этой функции):\n")
    _diagnose_brokers(cfg)
    return 0


def _diagnose_brokers(cfg) -> None:
    import socket

    from .together import broker_list, parse_broker, selfcheck

    for target in broker_list(cfg.together_broker):
        try:
            host, port, _tls = parse_broker(target)
            with socket.create_connection((host, port), timeout=5):
                print(f"✓ {host}:{port} доступен")
        except Exception as e:
            print(f"✗ {target}: {type(e).__name__}: {e}")
    print("\nСамопроверка: хост и гость на этом компьютере передают тестовый трек через сервер…")
    quiet = logging.getLogger("ymrpc.together")  # повторные попытки подключения не засоряют вывод
    level, quiet.level = quiet.level, logging.WARNING
    try:
        ok, lines = selfcheck(cfg.together_broker)
    finally:
        quiet.level = level
    for line in lines:
        print(line)
    print("\n" + ("✓ «Слушать вместе» должно работать." if ok else "✗ «Слушать вместе» пока не работает, см. сообщения выше."))


def importlib_missing(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is None
    except Exception:
        return True


# ---------------------------------------------------------------------------
class _NoTk:
    """Запасной «главный цикл», если в этом Python нет tkinter: трей и статус работают, окна настроек нет."""

    def __init__(self):
        self._quit = False

    def withdraw(self):
        pass

    def quit(self):
        self._quit = True

    def destroy(self):
        pass

    def after(self, _ms, fn):
        self._next = fn

    def mainloop(self):
        import time

        while not self._quit:
            time.sleep(0.15)
            fn, self._next = getattr(self, "_next", None), None
            if fn:
                fn()


def _open_config_file(store) -> None:
    """Без tkinter «Настройки» открывают config.json в блокноте."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(store.path)  # type: ignore[attr-defined]
    except Exception:
        log.exception("Не удалось открыть config.json")


def main(argv: list[str]) -> int:
    debug = "--debug" in argv
    setup_logging(debug)
    if "--diagnose" in argv:
        return diagnose()

    if sys.version_info < (3, 9):
        log.error("Нужен Python 3.9 или новее, сейчас %s. Запустите install.bat — он подберёт подходящий.",
                  sys.version.split()[0])
        return 1

    mutex = acquire_single_instance()
    if mutex is None:
        log.info("Программа уже запущена — второй экземпляр не нужен")
        return 0

    log.info("%s %s запускается (Python %s, аргументы: %s)", DISPLAY_NAME, __version__,
             sys.version.split()[0], argv or "нет")
    if autostart.FLAG in argv:
        log.info("Запуск при входе в Windows — жду готовности рабочего стола")
        autostart.wait_for_desktop()

    prepare_windows()

    try:
        import tkinter as tk
    except ImportError:  # бывает в урезанных сборках Python
        tk = None
        log.warning("В этом Python нет tkinter: окно настроек недоступно, настройки — в config.json")

    from .rpc import Worker
    from .status import Status
    from .tray import Tray

    store = ConfigStore()
    status = Status()
    worker = Worker(store, status)
    ui_queue: queue.Queue = queue.Queue()

    cfg = store.get()
    if cfg.autostart:
        autostart.set_enabled(True)  # обновит путь, если папку с программой переместили

    worker_thread = threading.Thread(target=worker.run_forever, name="worker", daemon=True)
    worker_thread.start()

    settings = None
    if tk is not None:
        from .icon import tk_png_data
        from .ui import SettingsWindow

        root = tk.Tk()
        root.withdraw()
        # Своя иконка во всех окнах вместо «пера» tkinter
        root._icons = [tk.PhotoImage(data=tk_png_data(size=s)) for s in (16, 32, 48, 64)]
        root.iconphoto(True, *root._icons)
        settings = SettingsWindow(root, store, worker)
    else:
        root = _NoTk()

    tray = Tray(store, status, worker, ui_queue)
    worker.together.add_listener(tray.refresh)
    threading.Thread(target=tray.run, name="tray", daemon=True).start()

    if store.first_run or not cfg.client_id:
        ui_queue.put(("settings", "first_run"))

    def copy_to_clipboard(text: str) -> None:
        if tk is None:
            return
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()

    def pump():
        try:
            while True:
                cmd, arg = ui_queue.get_nowait()
                if cmd == "settings":
                    if settings is not None:
                        settings.show(first_run=(arg == "first_run"), tab=arg if arg == "together" else None)
                    else:
                        _open_config_file(store)
                elif cmd == "copy":
                    copy_to_clipboard(arg)
                elif cmd == "quit":
                    root.quit()
                    return
        except queue.Empty:
            pass
        except Exception:
            log.exception("Ошибка в окне настроек")
        root.after(150, pump)

    root.after(150, pump)
    try:
        root.mainloop()
    finally:
        log.info("Выход…")
        tray.stop()
        worker.stop()
        worker_thread.join(timeout=6)
        try:
            root.destroy()
        except Exception:
            pass
    return 0
