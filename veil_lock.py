#!/usr/bin/env python3
# veil: lockscreen, ext-session-lock-v1 client for niri/wayland.
#
# order per the protocol spec:
#   - all prep (globals, fonts, pre-render) happens before lock()
#   - lock() -> get_lock_surface on every output right away
#   - configure -> ack -> attach -> commit
#   - `locked` only comes after frames are presented
#   - unlock_and_destroy only after locked (else invalid_unlock)
#
# if the client dies while locked, niri won't release the session
# (crimson screen, exit via TTY/reboot), so after lock()
# the code has no right to crash.
#
# run:    ~/.local/bin/veil-lock
# test:   veil_lock.py --test 5 (auto-unlock after 5s)
# render: veil_lock.py --render-out /tmp/frame.png (no wayland)

import argparse
import ctypes
import getpass
import mmap
import os
import platform
import random
import select
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/.local/share/veil"))

import psutil  # noqa: E402
import pam as pam_module  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# gruvbox palette
BG = "#282828"
FG = "#ebdbb2"
ACCENT = "#d79921"
BRIGHT = "#fab327"
MUTED = "#a89984"
DIM = "#665c54"
RED = "#fb4934"
GREEN = "#b8bb26"

FONT_DIR = os.path.expanduser("~/.local/share/fonts")
FONT_REG = os.path.join(FONT_DIR, "JetBrainsMonoNerdFont-Regular.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "JetBrainsMonoNerdFont-Bold.ttf")

_FONTS = {}


def get_font(bold: bool, size: int):
    key = (bold, size)
    if key not in _FONTS:
        path = FONT_BOLD if bold else FONT_REG
        _FONTS[key] = ImageFont.truetype(path, size)
    return _FONTS[key]


# ascii art "veil", dots between letters are mini-boxes
ART = [
    "██╗  ██╗  ███████╗  ██╗  ██╗         ",
    "██║  ██║  ██╔════╝  ██║  ██║         ",
    "██║  ██║  ███████╗  ██║  ██║         ",
    "╚██ ██╔╝  ██╔════╝  ██║  ██║         ",
    " ╚███╔╝   ███████╗  ██║  ███████╗ ██╗",
    "  ╚══╝    ╚══════╝  ╚═╝  ╚══════╝ ╚═╝",
]

_AUDIT_LINES = [
    "dumping cache... {}MB flushed",
    "verifying checksums... 0x{:08X} ok",
    "intercepting sigterm... rerouted to /dev/null",
    "masking process table... {} daemons hidden",
    "entropy pool reseed... {}%",
    "wayland socket guarded... ok",
    "scanning for rootkits... clean",
    "kernel keyring sealed... 0 keys leaked",
    "tty sniffers... none detected",
    "pam stack integrity... verified",
    "session-lock surface... inhibitor held",
]


def _audit_message() -> str:
    line = random.choice(_AUDIT_LINES)
    if "{}" in line:
        if "0x" in line:
            return line.format(random.randint(0, 0xFFFFFFFF))
        if "daemons" in line:
            return line.format(len(psutil.pids()))
        if "entropy" in line:
            return line.format(random.randint(88, 100))
        return line.format(random.randint(64, 512))
    return line


# US layout (evdev); veil-lock switches niri to US
_KEYMAP = {
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    12: "-", 13: "=", 16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u",
    23: "i", 24: "o", 25: "p", 26: "[", 27: "]", 30: "a", 31: "s", 32: "d", 33: "f",
    34: "g", 35: "h", 36: "j", 37: "k", 38: "l", 39: ";", 40: "'", 41: "`", 43: "\\",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m", 51: ",", 52: ".",
    53: "/", 57: " ",
    # numpad (evdev): KPENTER=96 is handled separately
    55: "*", 71: "7", 72: "8", 73: "9", 74: "-", 75: "4", 76: "5", 77: "6",
    78: "+", 79: "1", 80: "2", 81: "3", 82: "0", 83: ".", 98: "/",
}
_SHIFTMAP = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*",
    "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}", "\\": "|", ";": ":",
    "'": '"', "`": "~", ",": "<", ".": ">", "/": "?",
}
KEY_ESC, KEY_BACKSPACE, KEY_ENTER, KEY_KPENTER = 1, 14, 28, 96
MOD_SHIFT, MOD_CAPS, MOD_CTRL = 1, 2, 4


# --- state (wayland-independent) ---
class VeilState:
    def __init__(self):
        self.state = "locked"          # locked | verifying | failed | unlocked
        self.password = ""
        self.blink = True
        self.audit = deque(maxlen=3)
        self.cpu = 0.0
        self.ram = (0.0, 0.0)
        self.daemons = 0
        self.pam_done = None
        self.failed_until = 0.0
        psutil.cpu_percent(interval=None)
        self.refresh_telemetry()
        self.push_audit()
        self.push_audit()
        self.push_audit()

    def refresh_telemetry(self):
        self.cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        self.ram = (vm.used / 2**30, vm.total / 2**30)
        self.daemons = len(psutil.pids())

    def push_audit(self, msg=None):
        ts = datetime.now().strftime("%H:%M:%S")
        self.audit.append((ts, msg or _audit_message()))


# --- frame render (Pillow, char grid) ---
class Grid:
    def __init__(self, img, scale):
        self.img = img
        self.d = ImageDraw.Draw(img)
        fsize = max(8, int(21 * scale))
        self.font = get_font(False, fsize)
        self.font_bold = get_font(True, fsize)
        # float advance: int() drifted ~0.2px/column so the right
        # border slid a full cell off the corners by column 120
        self.cw = self.font.getlength("M") or 1.0
        asc, desc = self.font.getmetrics()
        self.ch = asc + desc
        W, H = img.size
        self.cols = int(W // self.cw)
        self.rows = H // self.ch
        self.ox = (W - self.cols * self.cw) / 2
        self.oy = (H - self.rows * self.ch) // 2

    def cell(self, col, row):
        return self.ox + col * self.cw, self.oy + row * self.ch

    def fill_cells(self, col, row, ncols, nrows, color):
        x, y = self.cell(col, row)
        self.d.rectangle(
            [x, y, x + ncols * self.cw - 1, y + nrows * self.ch - 1], fill=color
        )

    def text(self, col, row, s, fg=FG, bg=None, bold=False):
        if bg:
            self.fill_cells(col, row, len(s), 1, bg)
        x, y = self.cell(col, row)
        self.d.text((x, y), s, font=self.font_bold if bold else self.font, fill=fg)

    def box(self, col, row, w, h, title=None, fg=FG):
        self.text(col, row, "┌" + "─" * (w - 2) + "┐", fg=fg)
        mid = "│" + " " * (w - 2) + "│"  # one string = no seams
        for r in range(row + 1, row + h - 1):
            self.text(col, r, mid, fg=fg)
        self.text(col, row + h - 1, "└" + "─" * (w - 2) + "┘", fg=fg)
        if title:
            self.text(col + 2, row, f" {title} ", fg=ACCENT, bg=BG, bold=True)

    def bar(self, col, row, ncols, color, hfrac=0.68):
        """solid bar instead of separate chars (no spacing holes)."""
        x, y = self.cell(col, row)
        h = max(1, int(self.ch * hfrac))
        yo = (self.ch - h) // 2
        self.d.rectangle(
            [x, y + yo, x + ncols * self.cw - 1, y + yo + h - 1], fill=color
        )


def render_frame(state: VeilState, W, H, scale):
    img = Image.new("RGB", (W, H), BG)
    g = Grid(img, scale)
    C, R = g.cols, g.rows

    # header (inverted)
    kernel = platform.release()
    header = f"[ V.E.I.L. CORE TERMINAL ] -- KERNEL: {kernel} -- STATUS: ISOLATED"
    g.fill_cells(0, 0, C, 1, ACCENT)
    g.text(1, 0, header[: C - 2], fg=BG, bold=True)

    # panels
    footer_h = 5
    body_top, body_bot = 2, R - footer_h - 2
    body_h = body_bot - body_top
    tel_w = max(40, int(C * 0.30))
    g.box(2, body_top, tel_w, body_h, title="[ SYS_TELEMETRY ]")
    ax = 2 + tel_w + 1
    g.box(ax, body_top, C - ax - 2, body_h, title="[ AUTH_GATEWAY ]")
    g.box(2, R - footer_h - 1, C - 4, footer_h, title="[ BACKGROUND_AUDIT ]")

    # telemetry
    filled = max(0, min(10, round(state.cpu / 10)))
    tx, ty = 4, body_top + 2
    g.text(tx, ty, "CPU USAGE:", fg=FG)
    bx = tx + len("CPU USAGE:      ")
    g.text(bx, ty, "[" + "|" * filled, fg=BRIGHT)
    g.text(bx + 1 + filled, ty, "." * (10 - filled) + "]", fg=DIM)
    g.text(bx + 12, ty, f" {state.cpu:3.0f}%", fg=FG)
    g.text(tx, ty + 2, f"RAM ALLOC:      {state.ram[0]:.1f}G / {state.ram[1]:.1f}G", fg=FG)
    g.text(tx, ty + 4, "NET_TX/RX:      ", fg=FG)
    g.text(tx + 16, ty + 4, "DROP_ALL", fg=RED)
    g.text(tx, ty + 6, f"ACTIVE_DAEMONS: {state.daemons}", fg=FG)

    # AUTH_GATEWAY
    pw, ph = C - ax - 2, body_h
    art_x = ax + max(0, (pw - len(ART[0])) // 2)
    # content block: art(6) + 2 + status(1) + 2 + prompt(1) = 12 lines
    art_y = body_top + max(1, (ph - 12) // 2)
    for i, line in enumerate(ART):
        g.text(art_x, art_y + i, line, fg=BRIGHT, bold=True)

    if state.state == "locked":
        color = BRIGHT if state.blink else DIM
        msg = ">> VIRTUAL ENVIRONMENT ISOLATION LAYER: LOCKED <<"
    elif state.state == "verifying":
        color, msg = ACCENT, ">> VERIFYING USER_KEY..."
    elif state.state == "failed":
        color, msg = RED, "!! AUTH_FAILED_ // ACCESS_DENIED !!"
    else:
        color, msg = GREEN, "[ OK ] DECRYPTING SESSION..."
    g.text(ax + max(0, (pw - len(msg)) // 2), art_y + 8, msg, fg=color, bold=True)

    # input field: bar instead of blocks (solid, green) + cursor
    n = len(state.password)
    cursor = "▌" if state.blink and state.state == "locked" else " "
    prompt = "USER_KEY: "
    total = len(prompt) + max(n, 1) + 1
    px = ax + max(0, (pw - total) // 2)
    py = art_y + 11
    g.text(px, py, prompt, fg=ACCENT)
    if n:
        g.bar(px + len(prompt), py, n, FG)  # pale input bar
    g.text(px + len(prompt) + n, py, cursor, fg=BRIGHT)

    # audit log
    for i, (ts, msg) in enumerate(state.audit):
        row = R - footer_h + i
        g.text(4, row, f"[{ts}]", fg=DIM)
        g.text(15, row, msg[: C - 20], fg=MUTED)

    return img


# --- wayland client ---
def _memfd(size: int) -> int:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.memfd_create(b"veil-shm", 1)  # MFD_CLOEXEC
    if fd < 0:
        raise OSError(ctypes.get_errno(), "memfd_create failed")
    os.ftruncate(fd, size)
    return fd


class LockBusy(Exception):
    """compositor sent `finished`: session already taken by another locker."""


class OutputSurface:
    def __init__(self, veil, output, scale):
        self.veil = veil
        self.output = output
        self.scale = max(1, scale)
        self.w = self.h = 0
        self.configured = False
        self.lock_surface = None
        self.surface = None
        self.pool = None
        self.mm = None
        self.frame_size = 0
        self.buf_idx = 0
        self.busy = [False, False]
        # IMPORTANT: strong refs to wl_buffer: pywayland holds proxies
        # in a WeakSet, without a ref GC kills the proxy and the release
        # event has nowhere to go, busy sticks, screen freezes
        self.buffer_refs = [None, None]
        self._pools = []  # old pools live while their buffers do

    def attach(self, compositor, lock):
        self.surface = compositor.create_surface()
        self.lock_surface = lock.get_lock_surface(self.surface, self.output)
        self.lock_surface.dispatcher["configure"] = self.on_configure

    def on_configure(self, lock_surface, serial, w, h):
        try:
            lock_surface.ack_configure(serial)
            # configure gives LOGICAL size; smithay checks
            # buffer_size / buffer_scale == configure, so we render
            # the buffer in physical pixels (w*scale, h*scale)
            w, h = int(w), int(h)
            need_resize = (self.w, self.h) != (w, h)
            self.w, self.h = w, h
            if w <= 0 or h <= 0:
                return
            if self.pool is None or need_resize:
                # recreate the pool; keep the old one in _pools until the
                # compositor frees its buffers (else use-after-free
                # -> protocol error -> crimson screen)
                self.frame_size = w * self.scale * h * self.scale * 4
                fd = _memfd(self.frame_size * 2)
                mm = mmap.mmap(fd, self.frame_size * 2)
                pool = self.veil.shm.create_pool(fd, self.frame_size * 2)
                if self.pool is not None:
                    self._pools.append((self.pool, self.mm))
                self.pool, self.mm = pool, mm
                self.busy = [False, False]
                self.buffer_refs = [None, None]
                self.buf_idx = 0
            self.configured = True
            self.veil.redraw_now = True
        except Exception as e:  # after lock() we can't crash
            print(f"[V.E.I.L.] configure error: {e}", file=sys.stderr)

    def draw(self, state):
        if not self.configured:
            return
        pw, ph = self.w * self.scale, self.h * self.scale
        if pw * ph * 4 != self.frame_size:
            return  # size out of sync, wait for a new configure
        i = self.buf_idx
        if self.busy[i]:
            i = 1 - i
            if self.busy[i]:
                return
        img = render_frame(state, pw, ph, self.scale)
        off = i * self.frame_size
        self.mm.seek(off)
        self.mm.write(img.tobytes("raw", "BGRX"))
        buf = self.pool.create_buffer(off, pw, ph, pw * 4, 1)  # xrgb8888
        self.buffer_refs[i] = buf
        buf.dispatcher["release"] = self._make_release(i, buf)
        self.busy[i] = True
        self.surface.set_buffer_scale(self.scale)
        self.surface.attach(buf, 0, 0)
        self.surface.damage_buffer(0, 0, pw, ph)
        self.surface.commit()
        self.buf_idx = 1 - i

    def _make_release(self, i, buf):
        def _release(_b):
            self.busy[i] = False
            self.buffer_refs[i] = None
            try:
                buf.destroy()
            except Exception:
                pass
        return _release


class VeilLock:
    def __init__(self, test_seconds=0):
        self.state = VeilState()
        self.test_seconds = test_seconds
        self.surfaces = []
        self.shm = None
        self.compositor = None
        self.lock_manager = None
        self.keyboard = None
        self.outputs = {}          # proxy -> {"scale": int, "mode": (w,h)}
        self.lock = None
        self.locked_received = False
        self.finished = False
        self.running = True
        self.redraw_now = True
        self.mods = 0
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)

    def log(self, msg):
        print(f"[V.E.I.L.] {msg}", file=sys.stderr, flush=True)

    # phase 1: everything BEFORE lock()
    def connect(self):
        from pywayland.client import Display
        from protocols.wayland import WlCompositor, WlOutput, WlSeat, WlShm
        from protocols.ext_session_lock_v1 import ExtSessionLockManagerV1

        self.display = Display()
        self.display.connect()
        registry = self.display.get_registry()
        registry.dispatcher["global"] = self._on_global
        registry.dispatcher["global_remove"] = lambda *a: None
        self.display.roundtrip()   # global events -> binds
        self.display.roundtrip()   # events on bound proxies (caps, mode...)

        # finish waiting for the keyboard/output modes (up to 2s)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            if self.keyboard and all(o["mode"] for o in self.outputs.values()):
                break
            self.display.roundtrip()

        missing = [n for n, v in
                   (("compositor", self.compositor), ("shm", self.shm),
                    ("ext-session-lock", self.lock_manager)) if not v]
        if missing:
            raise RuntimeError(f"missing wayland globals: {missing}")
        if not self.outputs:
            raise RuntimeError("not a single wl_output")
        if not self.keyboard:
            raise RuntimeError("seat without a keyboard?")

    def _on_global(self, registry, name, interface, version):
        from protocols.wayland import WlCompositor, WlOutput, WlSeat, WlShm
        from protocols.ext_session_lock_v1 import ExtSessionLockManagerV1
        if interface == "wl_compositor":
            self.compositor = registry.bind(name, WlCompositor, min(version, 5))
        elif interface == "wl_shm":
            self.shm = registry.bind(name, WlShm, min(version, 2))
        elif interface == "wl_seat" and self.keyboard is None:
            seat = registry.bind(name, WlSeat, min(version, 8))
            seat.dispatcher["capabilities"] = self._make_seat_caps(seat)
            seat.dispatcher["name"] = lambda *a: None
        elif interface == "wl_output":
            out = registry.bind(name, WlOutput, min(version, 4))
            info = {"scale": 1, "mode": None}
            self.outputs[out] = info
            out.dispatcher["scale"] = self._make_scale(out, info)
            out.dispatcher["mode"] = self._make_mode(out, info)
            out.dispatcher["geometry"] = lambda *a: None
            out.dispatcher["done"] = lambda *a: None
            out.dispatcher["name"] = lambda *a: None
            out.dispatcher["description"] = lambda *a: None
        elif interface == "ext_session_lock_manager_v1":
            self.lock_manager = registry.bind(
                name, ExtSessionLockManagerV1, min(version, 1))

    def _make_scale(self, out, info):
        def _scale(_p, factor):
            info["scale"] = int(factor)
            for s in self.surfaces:
                if s.output is out:
                    s.scale = max(1, int(factor))
        return _scale

    def _make_mode(self, out, info):
        def _mode(_p, flags, w, h, _refresh):
            if flags & 0x1 or info["mode"] is None:  # current
                info["mode"] = (int(w), int(h))
        return _mode

    def _make_seat_caps(self, seat):
        def _caps(_seat, capabilities):
            if capabilities & 0x2 and self.keyboard is None:
                kb = seat.get_keyboard()
                kb.dispatcher["keymap"] = self._on_keymap
                kb.dispatcher["enter"] = lambda *a: None
                kb.dispatcher["leave"] = lambda *a: None
                kb.dispatcher["key"] = self._on_key
                kb.dispatcher["modifiers"] = self._on_modifiers
                kb.dispatcher["repeat_info"] = lambda *a: None
                self.keyboard = kb
        return _caps

    def _on_keymap(self, _kb, _fmt, fd, _size):
        try:
            os.close(fd)
        except OSError:
            pass

    def prewarm(self):
        """warm up fonts and render BEFORE lock(): after that we can't."""
        w, h = 1920, 1080
        scale = 1
        for info in self.outputs.values():
            if info["mode"]:
                w, h = info["mode"]
                scale = info["scale"]
                break
        get_font(False, max(8, int(21 * scale)))
        get_font(True, max(8, int(21 * scale)))
        t0 = time.monotonic()
        render_frame(self.state, w, h, scale)
        self.log(f"prewarm ok ({w}x{h}@{scale} in {time.monotonic()-t0:.2f}s)")

    # phase 2: lock() + surfaces IMMEDIATELY
    # WARNING: after sending lock() the process has no right to die
    # before getting `locked` + unlock_and_destroy. niri waits 1s
    # for frames (or their absence) and locks hard;
    # client death = crimson screen until reboot.
    def acquire_and_render(self):
        self.lock = self.lock_manager.lock()
        self.lock.dispatcher["locked"] = self._on_locked
        self.lock.dispatcher["finished"] = self._on_finished

        for out, info in self.outputs.items():
            s = OutputSurface(self, out, info["scale"])
            s.attach(self.compositor, self.lock)
            self.surfaces.append(s)
        self.display.flush()

        # wait for configure (we READ events: dispatch(block=True)!)
        fd = self.display.get_fd()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10.0:
            if self.finished:
                raise LockBusy()
            if all(s.configured for s in self.surfaces):
                break
            try:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    self.display.dispatch(block=True)
            except Exception as e:
                self.log(f"dispatch error: {e}")
                break
        if not all(s.configured for s in self.surfaces):
            self.log("configure not received, but continuing (niri will lock itself)")

        self._draw_all()

    def _on_locked(self, _lock):
        self.locked_received = True
        self.log("locked")

    def _on_finished(self, _lock):
        self.finished = True

    # input
    def _on_modifiers(self, _kb, _serial, depressed, latched, locked, _group):
        self.mods = int(depressed) | int(latched)
        self.locked_mods = int(locked)

    def _on_key(self, _kb, _serial, _time, key, key_state):
        try:
            if key_state != 1:
                return
            if key in (KEY_ESC, 29, 97, 56, 100):
                self.state.push_audit(f"intercepting key {key}... dropped")
                self.redraw_now = True
                return
            if self.mods & MOD_CTRL:
                self.state.push_audit("intercepting ctrl-sequence... dropped")
                self.redraw_now = True
                return
            if self.state.state != "locked":
                return
            if key == KEY_BACKSPACE:
                self.state.password = self.state.password[:-1]
            elif key in (KEY_ENTER, KEY_KPENTER):
                if self.state.password:
                    self.state.state = "verifying"
                    self._start_pam(self.state.password)
            else:
                ch = _KEYMAP.get(key)
                if ch is not None and len(self.state.password) < 64:
                    shifted = bool(self.mods & MOD_SHIFT)
                    if ch.isalpha():
                        caps = bool(getattr(self, "locked_mods", 0) & MOD_CAPS)
                        ch = ch.upper() if shifted != caps else ch
                    elif shifted:
                        ch = _SHIFTMAP.get(ch, ch)
                    self.state.password += ch
            self.redraw_now = True
        except Exception as e:
            self.log(f"key error: {e}")

    # PAM
    def _start_pam(self, password):
        def work():
            ok = _pam_auth(password)
            self.state.pam_done = ok
            try:
                os.write(self.wake_w, b"\x01")
            except OSError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _check_pam(self):
        res = self.state.pam_done
        if res is None:
            return
        self.state.pam_done = None
        if res:
            self.state.state = "unlocked"
            self.state.password = ""
            self.redraw_now = True
            self._draw_all()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.9:  # show "[ OK ]"
                try:
                    r, _, _ = select.select([self.display.get_fd()], [], [], 0.1)
                    if r:
                        self.display.dispatch(block=True)
                except Exception:
                    break
            self.unlock_confirmed()
        else:
            self.state.state = "failed"
            self.state.failed_until = time.monotonic() + 2.0
            self.redraw_now = True

    # main loop
    def run(self):
        self._install_signals()
        fd = self.display.get_fd()
        t_tel = t_blink = t_audit = time.monotonic()
        t_deadline = (time.monotonic() + self.test_seconds
                      if self.test_seconds else None)

        while self.running:
            now = time.monotonic()
            timeout = 0.05
            if self.state.state == "failed" and self.state.failed_until:
                timeout = max(0.0, min(timeout, self.state.failed_until - now))
            r, _, _ = select.select([fd, self.wake_r], [], [], timeout)

            if self.wake_r in r:
                try:
                    os.read(self.wake_r, 64)
                except OSError:
                    pass
                self._check_pam()
            if fd in r:
                self.display.dispatch(block=True)  # READS the socket, unlike block=False

            now = time.monotonic()
            if now - t_blink >= 0.5:
                t_blink = now
                self.state.blink = not self.state.blink
                self.redraw_now = True
            if now - t_tel >= 1.0:
                t_tel = now
                self.state.refresh_telemetry()
                self.redraw_now = True
            if now - t_audit >= 1.1:
                t_audit = now
                self.state.push_audit()
                self.redraw_now = True
            if (self.state.state == "failed" and self.state.failed_until
                    and now >= self.state.failed_until):
                self.state.failed_until = 0.0
                self.state.password = ""
                self.state.state = "locked"
                self.redraw_now = True
            if t_deadline and now >= t_deadline:
                if self.locked_received:
                    self.log("test deadline, unlocking")
                    self.unlock_confirmed()
                elif now - t_deadline < 0.1 or int(now) % 5 == 0:
                    self.log("test deadline: waiting for locked from the compositor...")
                t_deadline = now + 1.0 if not self.locked_received else t_deadline
            if self.redraw_now:
                self.redraw_now = False
                self._draw_all()

    def _install_signals(self):
        if self.test_seconds:
            # in test mode on SIGTERM/SIGINT: if locked arrived, honestly
            # unlock; if not, NO destroy at all, just wait
            def _handler(_sig, _frm):
                if self.locked_received:
                    self.unlock_confirmed()
                else:
                    self.log("TERM/INT before locked, can't destroy, waiting")
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    pass
            ignore = (signal.SIGTSTP, signal.SIGHUP, signal.SIGQUIT)
        else:
            ignore = (signal.SIGINT, signal.SIGTSTP, signal.SIGTERM,
                      signal.SIGHUP, signal.SIGQUIT)
        for sig in ignore:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass

    def _draw_all(self):
        for s in self.surfaces:
            try:
                s.draw(self.state)
            except Exception as e:
                self.log(f"draw error: {e}")
        try:
            self.display.flush()
        except Exception as e:
            self.log(f"flush error: {e}")

    # exits (all end with os._exit, bypassing cffi teardown)
    def unlock_confirmed(self):
        """only when locked was received: unlock_and_destroy is valid."""
        self.running = False
        try:
            self.lock.unlock_and_destroy()
            self.display.roundtrip()
        except Exception as e:
            self.log(f"unlock error: {e}")
        os._exit(0)


def _pam_auth(password: str) -> bool:
    try:
        user = getpass.getuser()
        if hasattr(pam_module, "pam"):
            return bool(pam_module.pam().authenticate(user, password))
        return bool(pam_module.authenticate(user, password))
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="V.E.I.L. ext-session-lock")
    ap.add_argument("--test", type=float, default=0,
                    help="auto-unlock after N seconds")
    ap.add_argument("--render-out", metavar="PNG",
                    help="render a frame to PNG and exit (no wayland)")
    args = ap.parse_args()

    if args.render_out:
        st = VeilState()
        img = render_frame(st, 3120, 2080, 2)
        img.save(args.render_out)
        print(f"frame {img.size} -> {args.render_out}")
        return 0

    veil = VeilLock(test_seconds=args.test)
    veil._install_signals()  # right away: in test mode TERM/INT = clean exit

    # phase 1: safe to crash
    try:
        veil.connect()
        veil.prewarm()
    except Exception as e:
        print(f"[V.E.I.L.] pre-lock error: {e}", file=sys.stderr)
        os._exit(1)

    # phase 2: after lock() crashing is forbidden
    try:
        veil.acquire_and_render()
    except LockBusy:
        print("[V.E.I.L.] session already locked (finished)", file=sys.stderr)
        os._exit(1)
    except Exception as e:
        # surfaces may have failed to create, don't exit! in 1s niri
        # locks itself (blank) and the keyboard keeps working:
        # password -> PAM -> unlock_and_destroy stays possible
        veil.log(f"acquire error: {e}, continuing anyway")

    fails = 0
    while True:
        try:
            veil.run()
            break
        except SystemExit:
            raise
        except Exception as e:
            fails += 1
            delay = min(float(fails), 10.0)  # backoff: don't spam the log
            veil.log(f"fatal in run(): {e}, restarting loop in {delay:.0f}s")
            time.sleep(delay)
    os._exit(0)


if __name__ == "__main__":
    main()
