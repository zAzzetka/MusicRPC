"""Окно настроек (tkinter — входит в стандартный Python)."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import DISPLAY_NAME, autostart
from .config import ConfigStore
from .together import DEFAULT_BROKERS, generate_code, normalize_code, selfcheck

log = logging.getLogger("ymrpc.ui")

DEV_PORTAL = "https://discord.com/developers/applications"

HOWTO_CLIENT_ID = (
    "Чтобы Discord показывал «Слушает …», нужно один раз создать своё приложение "
    "(это бесплатно, минута дела):\n\n"
    "1. Откроется сайт discord.com/developers/applications — нажмите «New Application».\n"
    "2. Назовите его как хотите — это название увидят друзья: «Слушает <название>». "
    "Например, «Музыка». Иконку можно загрузить в разделе General Information "
    "(готовая лежит в папке программы: assets\\app.png).\n"
    "3. На странице приложения скопируйте «ID приложения» (Application ID) — длинное число.\n"
    "4. Вставьте его в поле «Client ID» и нажмите «Сохранить»."
)

STATUS_LABELS = {
    "name": "Название приложения («Слушает …»)",
    "details": "Название трека",
    "state": "Исполнитель",
}

TOGETHER_MODES = (
    ("off", "Выключено"),
    ("host", "Я транслирую — друзья слушают то же, что и я (хост)"),
    ("guest", "Я слушаю друга — по его коду комнаты (гость)"),
)

TOGETHER_HELP = (
    "Как это работает. Хост включает режим «хост» — программа создаёт код комнаты. Код отправьте "
    "друзьям. Друзья выбирают «гость», вставляют код — и видят (и, если хотят, слышат) то же, что "
    "играет у хоста. Сообщения шифруются кодом комнаты; между вами только общий MQTT-сервер."
)


def _split(text: str) -> list:
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]


class SettingsWindow:
    """Одно окно настроек; повторный вызов просто выводит его на передний план."""

    def __init__(self, root: tk.Tk, store: ConfigStore, worker):
        self.root, self.store, self.worker = root, store, worker
        self.win: tk.Toplevel | None = None
        self._tabs: ttk.Notebook | None = None
        self._tab_frames: dict = {}

    def show(self, first_run: bool = False, tab: str | None = None) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self._bring_to_front()
            self._select(tab)
            return
        self._build()
        self._select(tab)
        if first_run:
            self.win.after(300, lambda: messagebox.showinfo(
                "Первый запуск", HOWTO_CLIENT_ID, parent=self.win))

    def _select(self, tab: str | None) -> None:
        if tab in self._tab_frames and self._tabs is not None:
            self._tabs.select(self._tab_frames[tab])

    def _bring_to_front(self) -> None:
        """Окно, открытое из трея, иначе может оказаться под другими окнами."""
        win = self.win
        win.lift()
        win.attributes("-topmost", True)
        win.after(300, lambda: win.winfo_exists() and win.attributes("-topmost", False))
        win.focus_force()

    # -- построение окна ----------------------------------------------------
    def _build(self) -> None:
        cfg = self.store.get()
        win = self.win = tk.Toplevel(self.root)
        win.title(f"{DISPLAY_NAME} — настройки")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        outer = ttk.Frame(win, padding=10)
        outer.grid(sticky="nsew")
        tabs = self._tabs = ttk.Notebook(outer)
        tabs.grid(row=0, column=0, sticky="nsew")
        self._tab_frames = {}
        for key, title, builder in (
            ("main", "Основное", self._tab_main),
            ("texts", "Тексты и фильтр", self._tab_texts),
            ("together", "Слушать вместе", self._tab_together),
        ):
            frame = ttk.Frame(tabs, padding=8)
            frame.columnconfigure(0, weight=1)
            tabs.add(frame, text=title)
            self._tab_frames[key] = frame
            builder(frame, cfg)

        row = ttk.Frame(outer)
        row.grid(row=1, column=0, sticky="e", pady=(10, 0))
        ttk.Button(row, text="Сохранить", command=self._save).pack(side="left", padx=4)
        ttk.Button(row, text="Отмена", command=win.destroy).pack(side="left")

        win.update_idletasks()
        self._bring_to_front()

    def _tab_main(self, tab, cfg) -> None:
        pad = {"padx": 4, "pady": 4}
        # --- Discord ---
        box = ttk.LabelFrame(tab, text="Discord", padding=8)
        box.grid(row=0, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Client ID:").grid(row=0, column=0, sticky="w")
        self.v_client = tk.StringVar(value=cfg.client_id)
        ttk.Entry(box, textvariable=self.v_client, width=34).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(box, text="Как получить?", command=self._howto).grid(row=0, column=2)
        self.v_enabled = tk.BooleanVar(value=cfg.enabled)
        ttk.Checkbutton(box, text="Показывать статус в Discord", variable=self.v_enabled).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- Источник ---
        box = ttk.LabelFrame(tab, text="Откуда брать музыку", padding=8)
        box.grid(row=1, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Приложения:").grid(row=0, column=0, sticky="w")
        self.v_filters = tk.StringVar(value=", ".join(cfg.app_filters))
        ttk.Entry(box, textvariable=self.v_filters, width=34).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(box, text="Проверить", command=self._check_sources).grid(row=0, column=2)
        ttk.Label(
            box, foreground="gray",
            text="Через запятую, достаточно части названия плеера: yandex, spotify, chrome.\n"
                 "Пусто — брать любой играющий плеер. Не знаете названия — нажмите «Проверить».",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # --- Что показывать ---
        box = ttk.LabelFrame(tab, text="Что показывать", padding=8)
        box.grid(row=2, column=0, sticky="ew", **pad)
        self.v_cover = tk.BooleanVar(value=cfg.show_cover)
        self.v_progress = tk.BooleanVar(value=cfg.show_progress)
        self.v_button = tk.BooleanVar(value=cfg.show_button)
        self.v_paused = tk.BooleanVar(value=cfg.show_paused)
        ttk.Checkbutton(box, text="Обложку альбома", variable=self.v_cover).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(box, text="Полосу прогресса / время", variable=self.v_progress).grid(row=0, column=1, sticky="w", padx=20)
        ttk.Checkbutton(box, text="Кнопку со ссылкой на трек", variable=self.v_button).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(box, text="Статус, когда трек на паузе", variable=self.v_paused).grid(row=1, column=1, sticky="w", padx=20)
        ttk.Label(box, foreground="gray", text="Кнопку видят другие люди, у себя в профиле вы её не увидите.").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- Прочее ---
        box = ttk.LabelFrame(tab, text="Прочее", padding=8)
        box.grid(row=3, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Обновлять раз в (сек):").grid(row=0, column=0, sticky="w")
        self.v_interval = tk.StringVar(value=str(cfg.poll_interval).rstrip("0").rstrip("."))
        ttk.Spinbox(box, from_=1, to=10, increment=1, width=6, textvariable=self.v_interval).grid(
            row=0, column=1, sticky="w", padx=6)
        self.v_autostart = tk.BooleanVar(value=cfg.autostart or autostart.is_enabled())
        ttk.Checkbutton(box, text="Запускать вместе с Windows", variable=self.v_autostart).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(box, text="Токен для поиска обложек:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_token = tk.StringVar(value=cfg.yandex_token)
        ttk.Entry(box, textvariable=self.v_token, width=34, show="•").grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Label(box, foreground="gray", text="Необязательно (OAuth-токен Яндекса). Нужен, только если обложки не находятся.").grid(
            row=3, column=0, columnspan=2, sticky="w")

    def _tab_texts(self, tab, cfg) -> None:
        pad = {"padx": 4, "pady": 4}
        box = ttk.LabelFrame(tab, text="Тексты", padding=8)
        box.grid(row=0, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        self.v_details = tk.StringVar(value=cfg.details_format)
        self.v_state = tk.StringVar(value=cfg.state_format)
        self.v_large = tk.StringVar(value=cfg.large_text_format)
        self.v_btn = tk.StringVar(value=cfg.button_label)
        for row, (label, var) in enumerate((
            ("Первая строка:", self.v_details),
            ("Вторая строка:", self.v_state),
            ("Подпись обложки:", self.v_large),
            ("Текст кнопки:", self.v_btn),
        )):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(box, textvariable=var, width=40).grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        ttk.Label(box, foreground="gray", text="Подстановки: {title} — трек, {artist} — исполнитель, {album} — альбом").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(2, 6))
        ttk.Label(box, text="В списке участников:").grid(row=5, column=0, sticky="w")
        self.v_status = tk.StringVar(value=STATUS_LABELS.get(cfg.status_display, STATUS_LABELS["name"]))
        ttk.Combobox(box, textvariable=self.v_status, state="readonly", values=list(STATUS_LABELS.values()),
                     width=38).grid(row=5, column=1, sticky="ew", padx=6)

        box = ttk.LabelFrame(tab, text="Не показывать", padding=8)
        box.grid(row=1, column=0, sticky="ew", **pad)
        box.columnconfigure(0, weight=1)
        self.v_hide = tk.StringVar(value=", ".join(cfg.hide_keywords))
        ttk.Entry(box, textvariable=self.v_hide).grid(row=0, column=0, sticky="ew")
        ttk.Label(box, foreground="gray", justify="left",
                  text="Слова через запятую. Если любое встретится в названии трека, исполнителе, альбоме\n"
                       "или названии плеера — статус не покажется и трек не попадёт в «слушать вместе».").grid(
            row=1, column=0, sticky="w", pady=(4, 0))

    def _tab_together(self, tab, cfg) -> None:
        pad = {"padx": 4, "pady": 4}
        ttk.Label(tab, foreground="gray", wraplength=690, justify="left", text=TOGETHER_HELP).grid(
            row=0, column=0, sticky="w", **pad)

        box = ttk.LabelFrame(tab, text="Режим", padding=8)
        box.grid(row=1, column=0, sticky="ew", **pad)
        self.v_tmode = tk.StringVar(value=cfg.together_mode)
        for row, (value, label) in enumerate(TOGETHER_MODES):
            ttk.Radiobutton(box, text=label, value=value, variable=self.v_tmode).grid(row=row, column=0, sticky="w")

        box = ttk.LabelFrame(tab, text="Комната", padding=8)
        box.grid(row=2, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Код комнаты:").grid(row=0, column=0, sticky="w")
        self.v_room = tk.StringVar(value=cfg.together_room)
        ttk.Entry(box, textvariable=self.v_room, width=22, font=("Consolas", 11)).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(box, text="Новый код", command=lambda: self.v_room.set(generate_code())).grid(row=0, column=2, padx=2)
        ttk.Button(box, text="Копировать", command=self._copy_room).grid(row=0, column=3, padx=2)
        ttk.Label(box, foreground="gray", text="Хост: нажмите «Новый код» и отправьте его друзьям. Гость: вставьте код друга.").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(box, text="Ваше имя:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_tname = tk.StringVar(value=cfg.together_name)
        ttk.Entry(box, textvariable=self.v_tname, width=22).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(box, foreground="gray", text="так вас увидят друзья").grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))

        box = ttk.LabelFrame(tab, text="Что делать гостю", padding=8)
        box.grid(row=3, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        self.v_mirror = tk.BooleanVar(value=cfg.together_mirror)
        self.v_syncp = tk.BooleanVar(value=cfg.together_sync_player)
        self.v_autoopen = tk.BooleanVar(value=cfg.together_auto_open)
        ttk.Checkbutton(box, text="Показывать трек хоста в моём Discord", variable=self.v_mirror).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(box, text="Подстраивать мой плеер (пауза, перемотка), если играет тот же трек",
                        variable=self.v_syncp).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(box, text="Открывать трек хоста в браузере, когда у меня играет другой",
                        variable=self.v_autoopen).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(box, text="Подпись в статусе:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.v_suffix = tk.StringVar(value=cfg.together_suffix)
        ttk.Entry(box, textvariable=self.v_suffix, width=28).grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(box, foreground="gray", text="{name} — имя хоста. Пусто — без подписи.").grid(
            row=4, column=0, columnspan=2, sticky="w")

        box = ttk.LabelFrame(tab, text="Сервер", padding=8)
        box.grid(row=4, column=0, sticky="ew", **pad)
        box.columnconfigure(0, weight=1)
        self.v_broker = tk.StringVar(value=cfg.together_broker)
        ttk.Entry(box, textvariable=self.v_broker).grid(row=0, column=0, sticky="ew")
        self.btn_check = ttk.Button(box, text="Проверить связь", command=self._check_together)
        self.btn_check.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(box, foreground="gray", justify="left",
                  text="Пусто — публичные MQTT-серверы по умолчанию (" + ", ".join(
                      b.split("://")[1].split(":")[0] for b in DEFAULT_BROKERS) + ").\n"
                       "Свой сервер: mqtt://адрес:1883 или mqtts://адрес:8883 (можно несколько через запятую).").grid(
            row=1, column=0, sticky="w", pady=(4, 0))

        self.v_tstatus = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.v_tstatus, foreground="#0a6").grid(row=5, column=0, sticky="w", padx=4, pady=(6, 0))
        self._refresh_together_status()

    def _refresh_together_status(self) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            self.v_tstatus.set(self.worker.together.describe())
        except Exception:
            pass
        self.win.after(1000, self._refresh_together_status)

    # -- действия -----------------------------------------------------------
    def _check_together(self) -> None:
        """Самопроверка: хост и гость на этом компьютере передают тестовый трек через сервер."""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def work(broker: str) -> None:
            try:
                fut.set_result(selfcheck(broker))
            except Exception as e:  # noqa: BLE001
                fut.set_exception(e)

        self.btn_check.state(["disabled"])
        self.v_tstatus.set("Проверяю связь с сервером, до полминуты…")
        threading.Thread(target=work, args=(self.v_broker.get().strip(),), daemon=True, name="together-check").start()

        def poll() -> None:
            if self.win is None or not self.win.winfo_exists():
                return
            if not fut.done():
                self.win.after(200, poll)
                return
            self.btn_check.state(["!disabled"])
            try:
                ok, lines = fut.result()
            except Exception as e:  # noqa: BLE001
                ok, lines = False, [f"Ошибка проверки: {e}"]
            text = "\n".join(lines) + ("\n\n«Слушать вместе» должно работать." if ok else
                                       "\n\n«Слушать вместе» пока не работает.")
            (messagebox.showinfo if ok else messagebox.showwarning)("Проверка связи", text, parent=self.win)

        self.win.after(200, poll)

    def _howto(self) -> None:
        webbrowser.open(DEV_PORTAL)
        messagebox.showinfo("Как получить Client ID", HOWTO_CLIENT_ID, parent=self.win)

    def _copy_room(self) -> None:
        room = normalize_code(self.v_room.get())
        if room is None:
            messagebox.showinfo("Код комнаты", "Сначала нажмите «Новый код» или вставьте код друга.", parent=self.win)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(room)
        self.root.update()
        messagebox.showinfo("Код комнаты", f"Скопировано: {room}", parent=self.win)

    def _check_sources(self) -> None:
        fut = self.worker.list_sources()

        def poll():
            if self.win is None or not self.win.winfo_exists():
                return
            if not fut.done():
                self.win.after(150, poll)
                return
            try:
                tracks = fut.result()
            except Exception as e:
                messagebox.showerror("Проверка источников", f"Не удалось прочитать медиа-центр Windows:\n{e}", parent=self.win)
                return
            if not tracks:
                text = ("Windows сейчас не видит ни одного плеера.\n\n"
                        "Запустите плеер и включите любой трек, затем повторите.")
            else:
                lines = []
                for t in tracks:
                    state = "играет" if t.playing else "пауза"
                    what = " — ".join(x for x in (t.artist, t.title) if x) or "(без названия)"
                    lines.append(f"• {t.app_id}\n    {what}  [{state}]")
                text = ("Windows видит такие источники:\n\n" + "\n".join(lines) +
                        "\n\nВ поле «Приложения» впишите часть названия нужного источника "
                        "(из строки с нужным плеером).")
            messagebox.showinfo("Проверка источников", text, parent=self.win)

        self.win.after(150, poll)

    def _save(self) -> None:
        client_id = self.v_client.get().strip()
        if client_id and not client_id.isdigit():
            messagebox.showwarning("Client ID", "Client ID — это число из 17–20 цифр (Application ID).\n"
                                   "Нажмите «Как получить?», если не знаете, где его взять.", parent=self.win)
            self._select("main")
            return
        try:
            interval = float(self.v_interval.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning("Интервал", "Интервал обновления должен быть числом от 1 до 10.", parent=self.win)
            self._select("main")
            return

        mode = self.v_tmode.get()
        room = normalize_code(self.v_room.get()) or ""
        if mode != "off" and not room:
            if mode == "host" and not self.v_room.get().strip():
                room = generate_code()  # хосту достаточно нажать «Сохранить» — код создадим сами
            else:
                messagebox.showwarning(
                    "Код комнаты",
                    "Код комнаты должен состоять из 12 символов, например ABCD-EFGH-JKLM.\n"
                    "Проверьте, что скопировали его целиком.", parent=self.win)
                self._select("together")
                return

        status_key = next((k for k, v in STATUS_LABELS.items() if v == self.v_status.get()), "name")
        old = self.store.get()
        # Меняем только то, что есть в окне: остальные настройки (в том числе будущие) не трогаем
        cfg = dataclasses.replace(
            old,
            client_id=client_id,
            enabled=self.v_enabled.get(),
            app_filters=_split(self.v_filters.get()),
            show_cover=self.v_cover.get(),
            show_progress=self.v_progress.get(),
            show_button=self.v_button.get(),
            show_paused=self.v_paused.get(),
            details_format=self.v_details.get(),
            state_format=self.v_state.get(),
            large_text_format=self.v_large.get(),
            button_label=self.v_btn.get(),
            status_display=status_key,
            hide_keywords=_split(self.v_hide.get()),
            together_mode=mode,
            together_room=room,
            together_name=self.v_tname.get(),
            together_broker=self.v_broker.get(),
            together_mirror=self.v_mirror.get(),
            together_sync_player=self.v_syncp.get(),
            together_auto_open=self.v_autoopen.get(),
            together_suffix=self.v_suffix.get(),
            poll_interval=interval,
            autostart=self.v_autostart.get(),
            yandex_token=self.v_token.get().strip(),
        )
        self.store.replace(cfg)
        if cfg.autostart != old.autostart or cfg.autostart:
            autostart.set_enabled(cfg.autostart)
        self.worker.wake()
        self.win.destroy()
