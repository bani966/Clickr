"""Clickr: hold the trigger, the virtual mouse clicks L or R until you let go."""
import ctypes
import json
import os
import queue
import random
import socket
import subprocess
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

import pystray
from PIL import Image, ImageChops, ImageDraw, ImageTk

from device import CONTROL_PORT as PORT, HOST

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(APP_DIR, "clickr.json")

KEY = "#ff00fe"  # painted transparent; that's how the window gets rounded corners
BG, SURFACE, OVERLAY = "#11111b", "#1e1e2e", "#313244"
TEXT, SUBTEXT = "#cdd6f4", "#7f849c"
CYAN, GREEN, RED, AMBER = "#33ccff", "#00ff99", "#f38ba8", "#f9e2af"
ACCENT = (CYAN, GREEN)
W, H = 380, 356


def run_clickr(command):
    """Runs clickr.ps1 <command>. Returns (exit code, last output line without its timestamp)."""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(APP_DIR, "clickr.ps1"), command]
    try:
        r = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    lines = [line.strip() for line in (r.stdout + r.stderr).splitlines() if line.strip()]
    return r.returncode, lines[-1].split(" ", 1)[-1] if lines else ""


class Client:
    def __init__(self):
        self.lock = threading.Lock()
        self.sock = None
        self.reader = None

    def connect(self, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.create_connection((HOST, PORT), timeout=2)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)

    def close(self):
        for f in (self.reader, self.sock):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        self.sock = self.reader = None

    def _exchange(self, cmd):
        self.sock.sendall((cmd + "\n").encode())
        reply = self.reader.readline()
        if not reply:
            raise ConnectionError("device closed the connection")
        return reply.strip()

    def send(self, cmd):
        with self.lock:
            try:
                if not self.sock:
                    self.connect(timeout=2)
                return self._exchange(cmd)
            except OSError:
                self.close()
                self.connect(timeout=2)
                return self._exchange(cmd)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetKeyNameTextW.argtypes = (wintypes.LONG, wintypes.LPWSTR, ctypes.c_int)

MOUSE_MESSAGES = {
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
}
MOUSE_NAMES = {"right": "right click", "middle": "middle click", "x1": "mouse 4 (rear)", "x2": "mouse 5 (front)"}
DEFAULT_TRIGGER = {"kind": "mouse", "code": "x1", "name": MOUSE_NAMES["x1"]}


def key_name(vk, scan, flags):
    buf = ctypes.create_unicode_buffer(64)
    if user32.GetKeyNameTextW((scan << 16) | ((flags & 1) << 24), buf, 64):
        return buf.value.lower()
    return f"key 0x{vk:02x}"


class TriggerHook:
    """System-wide mouse + keyboard hook. Eats the trigger so apps never see it.

    Every mouse event on the PC waits for handle() to return. Keep it instant: no Tk, no I/O, no locks.
    """

    WH_KEYBOARD_LL, WH_MOUSE_LL, WM_QUIT = 13, 14, 0x0012
    WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
    KEY_DOWN, KEY_UP = (0x0100, 0x0104), (0x0101, 0x0105)
    VK_ESCAPE = 0x1B

    def __init__(self, trigger, on_down, on_up, on_capture):
        self.trigger = trigger
        self.on_down, self.on_up, self.on_capture = on_down, on_up, on_capture
        self.capturing = False
        self.cancelled_at = 0.0
        self.just_captured = None
        self.tid = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def handle(self, kind, code, down, name):
        if self.capturing and down:
            self.capturing = False
            if (kind, code) in (("mouse", "left"), ("key", self.VK_ESCAPE)):
                # Left click can never be the trigger: eating it would make the PC unclickable.
                self.cancelled_at = time.monotonic()
                self.on_capture(None)
                return kind == "key"
            self.trigger = {"kind": kind, "code": code, "name": name()}
            self.just_captured = (kind, code)
            self.on_capture(self.trigger)
            return True
        if self.just_captured == (kind, code):
            # Still held from binding it. Without this, key auto-repeat fires it instantly.
            if not down:
                self.just_captured = None
            return True
        t = self.trigger
        if t and t["kind"] == kind and t["code"] == code:
            (self.on_down if down else self.on_up)()
            return True
        return False

    def _run(self):
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()

        @HOOKPROC
        def mouse_proc(n, wparam, lparam):
            if n == 0:
                info = MSLLHOOKSTRUCT.from_address(lparam)
                if wparam in (self.WM_XBUTTONDOWN, self.WM_XBUTTONUP):
                    button, down = ("x1" if info.mouseData >> 16 == 1 else "x2"), wparam == self.WM_XBUTTONDOWN
                else:
                    button, down = MOUSE_MESSAGES.get(wparam, (None, False))
                if button and not info.flags & 1 and self.handle(  # flag 1: injected by software, ignore
                        "mouse", button, down, lambda: MOUSE_NAMES.get(button, button)):
                    return 1
            return user32.CallNextHookEx(None, n, wparam, lparam)

        @HOOKPROC
        def key_proc(n, wparam, lparam):
            if n == 0 and wparam in self.KEY_DOWN + self.KEY_UP:
                info = KBDLLHOOKSTRUCT.from_address(lparam)
                if not info.flags & 0x10 and self.handle(  # flag 0x10: injected by software, ignore
                        "key", info.vkCode, wparam in self.KEY_DOWN,
                        lambda: key_name(info.vkCode, info.scanCode, info.flags)):
                    return 1
            return user32.CallNextHookEx(None, n, wparam, lparam)

        self._procs = (mouse_proc, key_proc)  # if these get garbage-collected, Windows calls freed memory
        hooks = [user32.SetWindowsHookExW(self.WH_MOUSE_LL, mouse_proc, None, 0),
                 user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, key_proc, None, 0)]
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for hook in hooks:
            user32.UnhookWindowsHookEx(hook)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, self.WM_QUIT, 0, 0)


def diag_gradient(size, c1, c2):
    t = Image.new("L", (64, 64))
    t.putdata([(x + y) * 255 // 126 for y in range(64) for x in range(64)])
    return Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), t.resize(size, Image.BILINEAR))


def rounded(w, h, r, fill, border=None, bw=1, outer=BG, key=None):
    ss = 4
    size = (w * ss, h * ss)
    shape = Image.new("L", size, 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), r * ss, fill=255)
    img = Image.new("RGB", size, fill)
    if border:
        c1, c2 = border if isinstance(border, tuple) else (border, border)
        b = bw * ss
        inner = Image.new("L", size, 0)
        ImageDraw.Draw(inner).rounded_rectangle((b, b, size[0] - 1 - b, size[1] - 1 - b), max(r - bw, 0) * ss, fill=255)
        img.paste(diag_gradient(size, c1, c2), (0, 0), ImageChops.subtract(shape, inner))
    img = img.resize((w, h), Image.LANCZOS)
    shape = shape.resize((w, h), Image.LANCZOS)
    out = Image.new("RGB", (w, h), outer)
    out.paste(img, (0, 0), shape)
    if key:
        out.paste(key, (0, 0, w, h), shape.point(lambda a: 255 if a < 96 else 0))
    return ImageTk.PhotoImage(out)


def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).rounded_rectangle((2, 2, 61, 61), 16, fill=255)
    img.paste(diag_gradient((64, 64), CYAN, GREEN), (0, 0), mask)
    ImageDraw.Draw(img).rounded_rectangle((8, 8, 55, 55), 11, fill=BG)
    return img


class App:
    def __init__(self):
        self.client = Client()
        self.jobs = queue.Queue()
        self.side_held = False
        self.config = self.load_config()
        self.action = self.config["action"]

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except OSError:
            pass
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-transparentcolor", KEY)
        root.bind("<Escape>", lambda e: self.hide())
        self.placed = False

        s = root.winfo_fpixels("1i") / 96
        px = self.px = lambda v: round(v * s)
        fams = set(tkfont.families())
        mono = self.mono = next((f for f in ("JetBrainsMono Nerd Font", "JetBrains Mono", "Cascadia Code", "Consolas") if f in fams), "Courier New")
        w, h = self.w, self.h = px(W), px(H)
        root.geometry(f"{w}x{h}")

        c = self.canvas = tk.Canvas(root, width=w, height=h, bg=KEY, highlightthickness=0, bd=0)
        c.pack()
        bw, bh, pw, ph = px(158), px(112), px(130), px(38)
        self.img = {
            "bg": rounded(w, h, px(14), BG, ACCENT, px(2), key=KEY),
            "normal": rounded(bw, bh, px(10), SURFACE, OVERLAY),
            "hover": rounded(bw, bh, px(10), SURFACE, ACCENT),
            "active": rounded(bw, bh, px(10), "#16303a", ACCENT, px(2)),
            "pill": rounded(pw, ph, px(10), SURFACE, OVERLAY),
            "pill_focus": rounded(pw, ph, px(10), SURFACE, ACCENT),
            "pill_wide": rounded(px(200), ph, px(10), SURFACE, OVERLAY),
            "pill_wide_focus": rounded(px(200), ph, px(10), SURFACE, ACCENT),
        }
        c.create_image(0, 0, anchor="nw", image=self.img["bg"], tags="drag")

        c.create_text(px(24), px(30), anchor="w", text="clickr", fill=TEXT, font=(mono, 12, "bold"), tags="drag")
        close = c.create_text(w - px(26), px(30), text="✕", fill=SUBTEXT, font=(mono, 11))
        c.tag_bind(close, "<Enter>", lambda e: c.itemconfig(close, fill=RED))
        c.tag_bind(close, "<Leave>", lambda e: c.itemconfig(close, fill=SUBTEXT))
        c.tag_bind(close, "<Button-1>", lambda e: self.hide())
        self.dot = c.create_oval(px(24), px(52), px(32), px(60), fill=AMBER, outline="")
        self.status = c.create_text(px(40), px(56), anchor="w", text="starting…", fill=SUBTEXT, font=(mono, 9))

        self.hover = {"L": False, "R": False}
        self.firing = {"L": False, "R": False}
        self.btn, self.letter = {}, {}
        for side, x in (("L", px(24)), ("R", w - px(24) - bw)):
            y = px(80)
            self.btn[side] = c.create_image(x, y, anchor="nw", image=self.img["normal"], tags=side)
            self.letter[side] = c.create_text(x + bw // 2, y + px(48), text=side, fill=TEXT, font=(mono, 28, "bold"), tags=side)
            c.create_text(x + bw // 2, y + px(88), text=f"{side}1 → wait → {side}2", fill=SUBTEXT, font=(mono, 9), tags=side)
            c.tag_bind(side, "<Enter>", lambda e, s=side: self.set_hover(s, True))
            c.tag_bind(side, "<Leave>", lambda e, s=side: self.set_hover(s, False))
            c.tag_bind(side, "<Button-1>", lambda e, s=side: self.select_action(s))
            self.redraw(side)

        # Plain floats copied from the fields. Worker threads read these; touching Tk from them stalls.
        self.delay, self.delta_ms = 0.015, 0.0
        self.interval = self.field("interval", px(226), self.config["interval"], lambda: self.update_timing())
        self.delta = self.field("delta ±", px(272), self.config["delta"], lambda: self.update_timing())
        self.update_timing()

        cy, tw = px(318), px(200)
        tx = w - px(24) - tw
        c.create_text(px(24), cy, anchor="w", text="trigger", fill=SUBTEXT, font=(mono, 10), tags="drag")
        self.trigger_pill = c.create_image(tx, cy, anchor="w", image=self.img["pill_wide"], tags="trigger")
        self.trigger_text = c.create_text(tx + tw // 2, cy, text=self.config["trigger"]["name"], fill=TEXT,
                                          font=(mono, 11), tags="trigger")
        c.tag_bind("trigger", "<Button-1>", lambda e: self.toggle_capture())

        c.tag_bind("drag", "<Button-1>", self.drag_start)
        c.tag_bind("drag", "<B1-Motion>", self.drag_move)

        self.hook = TriggerHook(self.config["trigger"], self.side_down, self.side_up,
                                lambda t: self.jobs.put(lambda: self.ui(self.capture_done, t)))
        self.icon = pystray.Icon("clickr", make_tray_image(), "Clickr", pystray.Menu(
            pystray.MenuItem("Show / hide", lambda: self.ui(self.toggle), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("L", lambda: self.ui(self.select_action, "L"), checked=lambda i: self.action == "L", radio=True),
            pystray.MenuItem("R", lambda: self.ui(self.select_action, "R"), checked=lambda i: self.action == "R", radio=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.ui(self.quit)),
        ))

    def ui(self, fn, *args):
        self.root.after(0, fn, *args)

    def set_status(self, text, color):
        self.canvas.itemconfig(self.status, text=text)
        self.canvas.itemconfig(self.dot, fill=color)

    def redraw(self, side):
        state = "active" if self.action == side else "hover" if self.hover[side] else "normal"
        self.canvas.itemconfig(self.btn[side], image=self.img[state])
        self.canvas.itemconfig(self.letter[side], fill=GREEN if self.firing[side] else TEXT)

    def set_hover(self, side, on):
        self.hover[side] = on
        self.redraw(side)

    def set_firing(self, side, on):
        self.firing[side] = on
        self.redraw(side)

    @staticmethod
    def conflicts(trigger, action):
        # Right-click trigger + R action: our own fake right clicks get eaten as triggers. Never allow it.
        return trigger["kind"] == "mouse" and trigger["code"] == "right" and action == "R"

    def select_action(self, side):
        if self.conflicts(self.hook.trigger, side):
            self.set_status("right click can't trigger R; pick another trigger first", RED)
            return
        old, self.action = self.action, side
        for s in (old, side):
            self.redraw(s)
        self.icon.update_menu()
        self.save_config()

    def toggle_capture(self):
        if time.monotonic() - self.hook.cancelled_at < 0.5:  # the hook already used this click to cancel
            return
        if self.hook.capturing:
            self.hook.capturing = False
            self.capture_done(None)
            return
        self.hook.capturing = True
        self.canvas.itemconfig(self.trigger_pill, image=self.img["pill_wide_focus"])
        self.canvas.itemconfig(self.trigger_text, text="press a button or key…", fill=AMBER)

    def capture_done(self, trigger):
        old = self.config["trigger"]
        if trigger and self.conflicts(trigger, self.action):
            self.hook.trigger = old
            self.set_status("right click can't trigger R; select L first", RED)
        elif trigger:
            self.config["trigger"] = trigger
            self.save_config()
            self.set_status(f"trigger → {trigger['name']}", GREEN)
        self.canvas.itemconfig(self.trigger_pill, image=self.img["pill_wide"])
        self.canvas.itemconfig(self.trigger_text, text=self.config["trigger"]["name"], fill=TEXT)

    @staticmethod
    def load_config():
        config = {"trigger": DEFAULT_TRIGGER, "action": "L", "interval": "15", "delta": "0"}
        try:
            with open(CONFIG, encoding="utf-8") as f:
                saved = json.load(f)
            config.update({k: saved[k] for k in config if k in saved})
        except (OSError, ValueError):
            pass
        if config["action"] not in ("L", "R"):
            config["action"] = "L"
        return config

    def save_config(self):
        self.config["action"] = self.action
        for key in ("interval", "delta"):
            if hasattr(self, key):
                self.config[key] = getattr(self, key).get()
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except OSError:
            pass

    def field(self, label, cy, default, on_change):
        c, px, pw = self.canvas, self.px, self.px(130)
        c.create_text(px(24), cy, anchor="w", text=label, fill=SUBTEXT, font=(self.mono, 10), tags="drag")
        pill_x = self.w - px(24) - pw
        pill = c.create_image(pill_x, cy, anchor="w", image=self.img["pill"])
        var = tk.StringVar(value=default)
        var.trace_add("write", lambda *a: on_change())
        entry = tk.Entry(c, textvariable=var, bg=SURFACE, fg=TEXT, insertbackground=CYAN, bd=0,
                         highlightthickness=0, justify="right", font=(self.mono, 12), selectbackground=OVERLAY)
        c.create_window(pill_x + px(14), cy, anchor="w", width=px(70), window=entry)
        c.create_text(pill_x + pw - px(14), cy, anchor="e", text="ms", fill=SUBTEXT, font=(self.mono, 10))
        entry.bind("<FocusIn>", lambda e: c.itemconfig(pill, image=self.img["pill_focus"]))
        entry.bind("<FocusOut>", lambda e: c.itemconfig(pill, image=self.img["pill"]))
        entry.bind("<MouseWheel>", lambda e: self.nudge(var, default, 1 if e.delta > 0 else -1))
        return var

    def update_timing(self):
        for var, apply in ((self.interval, lambda ms: setattr(self, "delay", ms / 1000)),
                           (getattr(self, "delta", None), lambda ms: setattr(self, "delta_ms", ms))):
            try:
                ms = float(var.get())
                if ms >= 0:
                    apply(ms)
            except (AttributeError, ValueError):
                pass
        if hasattr(self, "delta"):
            self.save_config()

    def jittered(self, base, floor):
        """base ± random(1, delta) ms. Delta under 1 means no jitter. Never below floor."""
        if self.delta_ms >= 1:
            base += random.choice((-1, 1)) * random.uniform(1, self.delta_ms) / 1000
        return max(base, floor)

    @staticmethod
    def nudge(var, default, step):
        try:
            var.set(str(max(0, int(float(var.get())) + step)))
        except ValueError:
            var.set(default)

    def drag_start(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")

    def toggle(self):
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def show(self):
        if not self.placed:
            r = wintypes.RECT()
            user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # work area, i.e. above the taskbar
            self.root.geometry(f"+{r.right - self.w - self.px(12)}+{r.bottom - self.h - self.px(12)}")
            self.placed = True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()

    def worker(self):
        while (job := self.jobs.get()) is not None:
            job()

    def send(self, cmd, report=True):
        try:
            reply = self.client.send(cmd)
            if report:  # off in the click loop: each Tk call from a thread costs up to ~20 ms
                self.ui(self.set_status, f"{cmd} → {reply}", GREEN)
            return True
        except Exception as e:
            self.ui(self.set_status, f"{cmd} failed: {e}", RED)
            return False

    def side_down(self):  # runs inside the hook: start a thread and get out
        if not self.side_held:
            self.side_held = True
            self.repeat_stop = threading.Event()
            threading.Thread(target=self.repeat, args=(self.action, self.repeat_stop), daemon=True).start()

    def side_up(self):
        if self.side_held:
            self.side_held = False
            self.repeat_stop.set()

    @staticmethod
    def wait(stop, seconds):
        end = time.perf_counter() + seconds
        while not stop.is_set() and (left := end - time.perf_counter()) > 0:
            time.sleep(min(left, 0.02))

    def repeat(self, side, stop):
        # Always ends on side2, so the button never stays stuck down.
        self.ui(self.set_firing, side, True)
        clicks = 0
        while not stop.is_set():
            # 1 ms floor: Windows reads the mouse every 1 ms. Faster just piles up clicks that fire after you let go.
            if not self.send(side + "1", report=False):
                break
            self.wait(stop, self.jittered(self.delay, 0.001))
            if not self.send(side + "2", report=False):
                break
            clicks += 1
            self.wait(stop, self.jittered(self.delay, 0.001))
        self.ui(self.set_firing, side, False)
        self.ui(self.set_status, f"{side} × {clicks} ({self.delay * 1000:g} ± {self.delta_ms:g} ms)", GREEN)

    def start(self):
        code, last = run_clickr("start")
        if code != 0:
            self.ui(self.set_status, last or f"start failed (exit {code})", RED)
            return
        try:
            self.client.connect()
            self.ui(self.set_status, "connected · " + self.client.send("status"), GREEN)
        except Exception as e:
            self.ui(self.set_status, f"connection failed: {e}", RED)

    def quit(self):
        self.set_status("stopping…", AMBER)
        self.root.update()
        self.hook.stop()
        try:
            self.client.send("release")
        except Exception:
            pass
        self.client.close()
        run_clickr("stop")
        self.jobs.put(None)
        self.icon.stop()
        self.root.destroy()

    def run(self):
        self.icon.run_detached()
        self.hook.start()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.start, daemon=True).start()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.quit()


if __name__ == "__main__":
    App().run()
