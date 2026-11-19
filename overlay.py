"""Локальное меню Ады (Windows): Ctrl+Alt+A открывает поле ввода в левом верхнем углу.

Enter отправляет запрос: окно плавно сжимается в квадратик со спиннером, а когда ответ готов,
разворачивается и показывает его целиком. Ответ виден только здесь, в Telegram он не уходит.
Ctrl+Alt+Space — запись голоса: первое нажатие начинает, второе распознаёт речь в поле ввода.
Esc или клик мимо окна закрывает поле ввода. Ответ висит REPLY_MS и закрывается сам или крестиком.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import math
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont

import voice

logger = logging.getLogger("Ada.Overlay")

MOD_ALT, MOD_CTRL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
VK_A = 0x41           # виртуальный код клавиши A: работает при любой раскладке
WM_HOTKEY = 0x0312
MARGIN = 16           # отступ окна от левого верхнего угла экрана
INPUT_W, INPUT_H = 680, 64
BUSY = 56             # сторона квадратика с анимацией загрузки
PAD = 18
FG = "#f2f3ff"
KEY = "#1c1e2c"       # цвет-ключ: эти пиксели прозрачны, сквозь них видно размытие
TINT = 0x992b1d1b     # AABBGGRR для акрила: тёмно-синяя тонировка, ~60% непрозрачности
REPLY_MS = 10_000     # сколько ответ висит на экране

_events: queue.Queue | None = None


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _CompositionData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def _apply_glass(hwnd: int):
    """Акриловое размытие под окном + скруглённые углы Windows 11."""
    accent = _AccentPolicy(4, 2, TINT, 0)  # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND
    data = _CompositionData(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
    ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    corner = ctypes.c_int(2)  # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))


def _force_foreground(hwnd: int):
    # Windows не отдаёт фокус фоновому процессу; нажатие Alt снимает этот запрет.
    user32 = ctypes.windll.user32
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def _hotkey_loop(events: queue.Queue):
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, 1, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, VK_A):
        logger.warning("Ctrl+Alt+A уже занята другой программой; локальное меню недоступно")
        return
    logger.info("Локальное меню: Ctrl+Alt+A")
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            events.put("toggle")


def _ui_loop(events: queue.Queue, on_submit, transcribe):
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # иначе Windows растягивает окно и текст мылится
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=KEY)
    root.attributes("-transparentcolor", KEY)
    root.geometry(f"{INPUT_W}x{INPUT_H}+{MARGIN}+{MARGIN}")

    font = tkfont.Font(family="Segoe UI", size=12)
    entry = tk.Entry(root, font=("Segoe UI", 16), bg=KEY, fg=FG, insertbackground=FG,
                     disabledbackground=KEY, disabledforeground="#9aa0c0", relief="flat", bd=0,
                     highlightthickness=0)
    spinner = tk.Canvas(root, width=BUSY, height=BUSY, bg=KEY, highlightthickness=0)
    arc = spinner.create_arc(16, 16, BUSY - 16, BUSY - 16, start=0, extent=100, style="arc", outline=FG, width=3)
    reply = tk.Text(root, font=font, bg=KEY, fg=FG, relief="flat", bd=0, highlightthickness=0,
                    wrap="word", cursor="arrow", selectbackground="#4a4f7a")
    # Фон не KEY: прозрачные пиксели пропускают клики насквозь, и в крестик было бы трудно попасть.
    close = tk.Label(root, text="✕", font=("Segoe UI", 11), bg="#3a3d5c", fg=FG, cursor="hand2", padx=6)
    widgets = (entry, spinner, reply)
    entry.pack(fill="both", expand=True, padx=22, pady=14)
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    _apply_glass(hwnd)

    recorder = voice.Recorder()
    state = {"mode": "hidden", "anim": None, "angle": 0, "timer": None}  # mode: hidden | input | busy | reply

    def cancel_timer():
        if state["timer"]:
            root.after_cancel(state["timer"])
            state["timer"] = None

    def use(widget=None, **pack):
        for w in widgets:
            w.pack_forget()
        close.place_forget()
        if widget is not None:
            widget.pack(fill="both", expand=True, **pack)

    def animate(width: int, height: int, done, steps: int = 14):
        """Плавно меняет размер окна (угол остаётся на месте), потом вызывает done()."""
        if state["anim"]:
            root.after_cancel(state["anim"])
        use()
        w0, h0 = root.winfo_width(), root.winfo_height()

        def step(i=1):
            t = 1 - (1 - i / steps) ** 3  # ease-out
            root.geometry(f"{round(w0 + (width - w0) * t)}x{round(h0 + (height - h0) * t)}+{MARGIN}+{MARGIN}")
            if i < steps:
                state["anim"] = root.after(15, step, i + 1)
            else:
                state["anim"] = None
                done()
        step()

    def spin():
        if state["mode"] == "busy":
            state["angle"] = (state["angle"] - 12) % 360
            spinner.itemconfigure(arc, start=state["angle"])
            root.after(30, spin)

    def set_status(text: str):
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.configure(state="disabled")

    def show_input():
        if state["anim"]:
            root.after_cancel(state["anim"])
            state["anim"] = None
        cancel_timer()
        state["mode"] = "input"
        root.geometry(f"{INPUT_W}x{INPUT_H}+{MARGIN}+{MARGIN}")
        use(entry, padx=22, pady=14)
        entry.configure(state="normal")
        entry.delete(0, "end")
        root.deiconify()
        root.lift()
        _force_foreground(hwnd)
        entry.focus_force()

    def hide(_event=None):
        if state["mode"] == "busy":
            return  # ждём ответ: квадратик со спиннером остаётся на экране
        if recorder.active:
            recorder.cancel()
        cancel_timer()
        if state["anim"]:
            root.after_cancel(state["anim"])
            state["anim"] = None
        root.withdraw()
        state["mode"] = "hidden"

    def submit(_event=None):
        text = entry.get().strip()
        if entry.cget("state") == "disabled" or not text:
            return
        state["mode"] = "busy"
        animate(BUSY, BUSY, lambda: (use(spinner), spin()))
        on_submit(text)

    def show_reply(text: str):
        cancel_timer()
        state["mode"] = "reply"
        root.deiconify()  # show_on_desktop может прийти, когда окно скрыто
        root.lift()
        reply.configure(state="normal")
        reply.delete("1.0", "end")
        reply.insert("1.0", text)
        reply.configure(state="disabled")
        reply.yview_moveto(0)
        # Высота под весь текст; запас 15% на перенос по словам. Не влезает в экран — колесо мыши прокручивает.
        inner = INPUT_W - 2 * PAD
        lines = sum(max(1, math.ceil(font.measure(line) * 1.15 / inner)) for line in text.split("\n"))
        height = min(lines * font.metrics("linespace") + 2 * PAD + 6, root.winfo_screenheight() - 2 * MARGIN - 60)

        def done():
            use(reply, padx=PAD, pady=PAD)
            close.place(relx=1, x=-6, y=6, anchor="ne")
            state["timer"] = root.after(REPLY_MS, hide)
        animate(INPUT_W, height, done)

    def toggle_voice(_event=None):
        if not recorder.active:
            try:
                recorder.start()
            except OSError as e:
                logger.error("Не удалось начать запись: %s", e)
                set_status(f"Микрофон недоступен: {e}")
                root.after(2000, show_input)
                return "break"
            set_status("🎙 Говори… (Ctrl+Alt+Space — готово)")
            return "break"
        audio = recorder.stop()
        set_status("Распознаю…")

        def work():
            try:
                events.put(("voice", transcribe(audio)))
            except Exception as e:
                logger.exception("Ошибка распознавания речи")
                events.put(("voice_error", str(e)))
        threading.Thread(target=work, daemon=True).start()
        return "break"

    def poll():
        while not events.empty():
            event = events.get()
            if event == "toggle":
                if state["mode"] == "input":
                    hide()
                elif state["mode"] != "busy":
                    show_input()
            elif event[0] == "reply":
                show_reply(event[1])
            elif state["mode"] == "input" and event[0] == "voice":
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, event[1])
                entry.icursor("end")
            elif state["mode"] == "input" and event[0] == "voice_error":
                set_status(f"Не распознала: {event[1][:80]}")
                root.after(2000, show_input)
        root.after(50, poll)

    def on_focus_out(_event):
        # Прячем, только когда фокус ушёл из приложения целиком (focus_get() is None), а не внутри окна.
        root.after(150, lambda: hide() if state["mode"] == "input"
                   and root.focus_get() is None and not recorder.active else None)

    entry.bind("<Return>", submit)
    entry.bind("<Control-Alt-space>", toggle_voice)
    entry.bind("<Escape>", hide)
    entry.bind("<FocusOut>", on_focus_out)
    close.bind("<Button-1>", hide)
    poll()
    root.mainloop()


def show_reply(text: str):
    """Показать ответ Ады в окне; можно вызывать из любого потока."""
    if _events is not None:
        _events.put(("reply", text))


def start(on_submit, transcribe):
    """on_submit(text) и transcribe(wav_bytes) -> str вызываются из потоков окна."""
    global _events
    _events = queue.Queue()
    threading.Thread(target=_hotkey_loop, args=(_events,), daemon=True, name="ada-hotkey").start()
    threading.Thread(target=_ui_loop, args=(_events, on_submit, transcribe), daemon=True, name="ada-overlay").start()
    return _events
