# veil

локскрин для niri/wayland на ext-session-lock-v1: python + pywayland,
рендер кадра через pillow, пароль проверяется через PAM. сессию держит
композитор, поэтому Ctrl+C и kill не помогут — если процесс умрёт
залоченным, разблокировать придётся из TTY (см. ниже).

на экране: ascii-арт, часы, cpu/ram, фейковый «аудит» внизу — просто
потому что выглядит круто. палитра gruvbox.

## установка

    ./install.sh

скрипт ставит всё в ~/.local/share/veil (venv, биндинги протоколов из xml/),
лаунчер — в ~/.local/bin/veil-lock. бинд в niri:

    Mod+Shift+V { spawn "/home/anaidyss/.local/bin/veil-lock"; }

нужны: python3, шрифт JetBrainsMono Nerd Font в ~/.local/share/fonts
(Regular и Bold ttf).

## как работает

- вся подготовка (глобалы, шрифты, пре-рендер) — строго до lock()
- после lock() сразу get_lock_surface на каждый output, кадры через shm/memfd
- `locked` приходит только после предъявления кадров
- unlock_and_destroy — только после locked, иначе invalid_unlock и бордовый экран
- аварийный выход без ребута: Ctrl+Alt+F3, логин в TTY, затем
  `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 hyprlock`

биндинги протоколов не лежат в репо, они генерируются install.sh:

    venv/bin/python -m pywayland.scanner -i xml/wayland.xml xml/ext-session-lock-v1.xml -o protocols

## veil_ui.py

старая версия — TUI на textual для обычного терминала (не настоящий локскрин,
для tty). deps: python3-textual python3-psutil python3-pam, запуск напрямую.
