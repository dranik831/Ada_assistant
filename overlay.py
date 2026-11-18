"""Локальное меню Ады (Windows): Win+Shift+A открывает окно ввода, Enter отправляет запрос.

Окно без рамки, полупрозрачное, с акриловым размытием и скруглёнными углами (DWM).
Ctrl+Space в окне — запись голоса: первое нажатие начинает, второе распознаёт речь в поле ввода.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import threading
import tkinter as tk

import voice

logger = logging.getLogger("Ada.Overlay")

MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
WIDTH, HEIGHT = 680, 64
BG = "#1b1d2b"        # тонировка окна
KEY = "#1c1e2c"       # цвет-ключ: эти пиксели прозрачны, сквозь них видно размытие
TINT = 0x992b1d1b     # AABBGGRR для акрила: тёмно-синяя тонировка, ~60% непрозрачности
PLACEHOLDER = "Спроси Аду…"


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
    if not user32.RegisterHotKey(None, 1, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, ord("A")):
        logger.warning("Win+Shift+A уже занята другой программой; локальное меню недоступно")
        return
    logger.info("Локальное меню: Win+Shift+A")
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
    x = (root.winfo_screenwidth() - WIDTH) // 2
    root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{root.winfo_screenheight() // 3}")

    entry = tk.Entry(root, font=("Segoe UI", 16), bg=KEY, fg="#f2f3ff", insertbackground="#f2f3ff",
                     disabledbackground=KEY, disabledforeground="#9aa0c0", relief="flat", bd=0,
                     highlightthickness=0)
    entry.pack(fill="both", expand=True, padx=22, pady=14)
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    _apply_glass(hwnd)

    recorder = voice.Recorder()
    state = {"visible": False}

    def set_status(text: str):
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.configure(state="disabled")

    def show():
        entry.configure(state="normal")
        entry.delete(0, "end")
        root.deiconify()
        root.lift()
        _force_foreground(hwnd)
        entry.focus_force()
        state["visible"] = True

    def hide(_event=None):
        if recorder.active:
            recorder.cancel()
        root.withdraw()
        state["visible"] = False

    def submit(_event=None):
        text = entry.get().strip()
        if entry.cget("state") == "disabled" or not text:
            return
        hide()
        on_submit(text)

    def toggle_voice(_event=None):
        if not recorder.active:
            try:
                recorder.start()
            except OSError as e:
                logger.error("Не удалось начать запись: %s", e)
                set_status(f"Микрофон недоступен: {e}")
                root.after(2000, show)
                return "break"
            set_status("🎙 Говори… (Ctrl+Space — готово)")
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
                hide() if state["visible"] else show()
            elif state["visible"] and event[0] == "voice":
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, event[1])
                entry.icursor("end")
            elif state["visible"] and event[0] == "voice_error":
                set_status(f"Не распознала: {event[1][:80]}")
                root.after(2000, show)
        root.after(50, poll)

    entry.bind("<Return>", submit)
    entry.bind("<Escape>", hide)
    entry.bind("<Control-space>", toggle_voice)
    # Прячем, только когда фокус ушёл из приложения целиком (focus_get() is None), а не внутри окна.
    entry.bind("<FocusOut>", lambda e: root.after(
        150, lambda: hide() if state["visible"] and root.focus_get() is None and not recorder.active else None))
    poll()
    root.mainloop()


def start(on_submit, transcribe):
    """on_submit(text) и transcribe(wav_bytes) -> str вызываются из потоков окна."""
    events: queue.Queue = queue.Queue()
    threading.Thread(target=_hotkey_loop, args=(events,), daemon=True, name="ada-hotkey").start()
    threading.Thread(target=_ui_loop, args=(events, on_submit, transcribe), daemon=True, name="ada-overlay").start()
    return events
