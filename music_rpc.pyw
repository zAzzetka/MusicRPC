"""Запуск без окна консоли: двойной клик по файлу или start.bat.

Для диагностики: python music_rpc.pyw --diagnose
"""
import datetime
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _crash_log(text: str) -> None:
    """Без консоли ошибку запуска иначе не увидеть — пишем её в файл."""
    try:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "MusicRPC"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n{text}")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        from ymrpc.app import main

        code = main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        _crash_log(traceback.format_exc())
        raise
    sys.exit(code)
