#!/usr/bin/env python3
# veil — терминальный локскрин (textual), старая версия для TTY
# deps: sudo dnf install python3-textual python3-psutil python3-pam
# цвета gruvbox: фон #282828, рамки #ebdbb2, акценты #d79921 #fab327

import asyncio
import getpass
import platform
import random
import signal
import sys
from collections import deque
from datetime import datetime

try:
    import psutil
    import pam as pam_module
    from textual.app import App
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Static
except ImportError as exc:
    print(f"[V.E.I.L.] Не хватает зависимости: {exc}")
    print("[V.E.I.L.] Установи: sudo dnf install python3-textual python3-psutil python3-pam")
    raise SystemExit(1)

# ASCII-арт "V.E.I.L." блочным шрифтом
_GLYPHS = {
    "V": ["██     ██", " ██   ██ ", " ██   ██ ", "  ██ ██  ", "   ███   "],
    "E": ["██████", "██    ", "█████ ", "██    ", "██████"],
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
    "L": ["██    ", "██    ", "██    ", "██    ", "██████"],
    ".": [" ", " ", " ", " ", "█"],
}
_ART = "\n".join(
    " ".join(_GLYPHS[ch][row] for ch in "V.E.I.L.")
    for row in range(5)
)

# Фоновый «аудит» для нижней панели
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


class VeilApp(App):
    """V.E.I.L. — пока PAM не скажет True, выхода нет."""

    TITLE = "V.E.I.L."
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = []  # никаких хоткеев textual (ctrl+q и т.п.)

    CSS = """
    Screen {
        background: #282828;
    }
    #header {
        dock: top;
        height: 1;
        background: #d79921;
        color: #282828;
        text-style: bold;
        padding: 0 1;
    }
    #main {
        height: 1fr;
    }
    #telemetry {
        width: 30%;
        height: 100%;
        border: solid #ebdbb2;
        border-title-color: #d79921;
        border-title-style: bold;
        padding: 1 1;
        color: #ebdbb2;
    }
    #auth {
        width: 70%;
        height: 100%;
        border: solid #ebdbb2;
        border-title-color: #d79921;
        border-title-style: bold;
        align: center middle;
    }
    #art {
        width: 38;
        height: 5;
        color: #fab327;
        text-style: bold;
    }
    #status {
        height: 1;
        margin-top: 2;
        text-style: bold;
    }
    #prompt {
        height: 1;
        margin-top: 2;
    }
    #footer {
        dock: bottom;
        height: 5;
        border: solid #ebdbb2;
        border-title-color: #d79921;
        border-title-style: bold;
        padding: 0 1;
        color: #a89984;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._password = ""
        self._state = "locked"  # locked | verifying | failed | unlocked
        self._blink = True
        self._audit = deque(maxlen=3)

    # Разметка
    def compose(self):
        kernel = platform.release()
        yield Static(
            f"\\[ V.E.I.L. CORE TERMINAL ] -- KERNEL: {kernel} -- STATUS: ISOLATED",
            id="header",
        )
        with Horizontal(id="main"):
            yield Static("", id="telemetry")
            with Vertical(id="auth"):
                yield Static(_ART, id="art")
                yield Static("", id="status")
                yield Static("", id="prompt")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self.query_one("#telemetry").border_title = "\\[ SYS_TELEMETRY ]"
        self.query_one("#auth").border_title = "\\[ AUTH_GATEWAY ]"
        self.query_one("#footer").border_title = "\\[ BACKGROUND_AUDIT ]"

        # Перехват сигналов ещё раз: драйвер textual мог поставить свои
        for sig in (signal.SIGINT, signal.SIGTSTP, signal.SIGTERM,
                    signal.SIGHUP, signal.SIGQUIT):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        psutil.cpu_percent(interval=None)  # прогреть счётчик CPU
        self._update_telemetry()
        self._update_status()
        self._update_prompt()
        self._push_audit()
        self.set_interval(1.0, self._update_telemetry)
        self.set_interval(0.5, self._tick_blink)
        self.set_interval(1.1, self._push_audit)

    # не даём textual усыпить/закрыть приложение
    def action_quit(self) -> None:
        pass

    def action_suspend(self) -> None:
        pass

    # Телеметрия (левая колонка)
    def _update_telemetry(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        filled = max(0, min(10, round(cpu / 10)))
        bar = f"[#fab327]{'|' * filled}[/][#665c54]{'.' * (10 - filled)}[/]"
        vm = psutil.virtual_memory()
        ram = f"{vm.used / 2**30:.1f}G / {vm.total / 2**30:.1f}G"
        daemons = len(psutil.pids())
        self.query_one("#telemetry", Static).update(
            f"CPU USAGE:      \\[{bar}] {cpu:3.0f}%\n"
            f"RAM ALLOC:      {ram}\n"
            f"NET_TX/RX:      [#fb4934]DROP_ALL[/]\n"
            f"ACTIVE_DAEMONS: {daemons}"
        )

    # Статус (мигающий LOCKED / ошибки / успех)
    def _tick_blink(self) -> None:
        self._blink = not self._blink
        self._update_status()
        self._update_prompt()

    def _update_status(self) -> None:
        widget = self.query_one("#status", Static)
        if self._state == "locked":
            color = "#fab327" if self._blink else "#665c54"
            widget.update(
                f"[{color}]>> VIRTUAL ENVIRONMENT ISOLATION LAYER: LOCKED <<[/]"
            )
        elif self._state == "verifying":
            widget.update("[#d79921]>> VERIFYING USER_KEY...[/]")
        elif self._state == "failed":
            widget.update("[#fb4934]!! AUTH_FAILED_ // ACCESS_DENIED !![/]")
        elif self._state == "unlocked":
            widget.update("[#b8bb26]\\[ OK ] DECRYPTING SESSION...[/]")

    def _update_prompt(self) -> None:
        blocks = "█" * len(self._password)
        cursor = "▌" if self._blink and self._state == "locked" else " "
        self.query_one("#prompt", Static).update(
            f"[#d79921]USER_KEY:[/] [#ebdbb2]{blocks}[/][#fab327]{cursor}[/]"
        )

    # Фоновый «аудит» (нижняя панель)
    def _push_audit(self) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._audit.append(f"[#665c54]\\[{ts}][/] {_audit_message()}")
        self.query_one("#footer", Static).update("\n".join(self._audit))

    # Ввод: всё перехватывается здесь
    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()

        if event.key in ("ctrl+c", "ctrl+z", "ctrl+q", "ctrl+d", "escape"):
            self._audit_intercept(event.key)
            return

        if self._state != "locked":
            return

        if event.key == "backspace":
            self._password = self._password[:-1]
            self._update_prompt()
        elif event.key == "enter":
            if self._password:
                self._state = "verifying"
                self._update_status()
                self.run_worker(self._authenticate(self._password))
        elif event.character and event.is_printable:
            if len(self._password) < 64:
                self._password += event.character
                self._update_prompt()

    def _audit_intercept(self, key: str) -> None:
        name = key.replace("ctrl+", "sig").replace("escape", "esc")
        ts = datetime.now().strftime("%H:%M:%S")
        self._audit.append(f"[#665c54]\\[{ts}][/] intercepting {name}... dropped")
        self.query_one("#footer", Static).update("\n".join(self._audit))

    # PAM-аутентификация
    @staticmethod
    def _pam_auth(password: str) -> bool:
        try:
            user = getpass.getuser()
            if hasattr(pam_module, "pam"):  # python-pam 1.x и 2.x
                return bool(pam_module.pam().authenticate(user, password))
            return bool(pam_module.authenticate(user, password))
        except Exception:
            return False

    async def _authenticate(self, password: str) -> None:
        ok = await asyncio.to_thread(self._pam_auth, password)
        if ok:
            self._state = "unlocked"
            self._password = ""
            self._update_status()
            self._update_prompt()
            await asyncio.sleep(0.8)
            self.exit(return_code=0)
        else:
            self._state = "failed"
            self._update_status()
            await asyncio.sleep(2.0)
            self._password = ""
            self._state = "locked"
            self._update_status()
            self._update_prompt()


def main() -> None:
    # Игнорируем сигналы до старта: Ctrl+C / Ctrl+Z / kill не помогут
    for sig in (signal.SIGINT, signal.SIGTSTP, signal.SIGTERM,
                signal.SIGHUP, signal.SIGQUIT):
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass

    app = VeilApp()
    app.run()
    sys.exit(app.return_code or 0)


if __name__ == "__main__":
    main()
