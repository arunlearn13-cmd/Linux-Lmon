#!/usr/bin/env sh
# Install the `lmon` command (like btop/atop): run `lmon` from any terminal afterwards.
# Re-run this script after changing the code to update the installed copy.
set -e
cd "$(dirname "$0")"
if command -v pipx >/dev/null 2>&1; then
    pipx install --force .
    bin="$HOME/.local/bin/lmon"
else
    # no pipx: private venv + symlink into ~/.local/bin
    dest="$HOME/.local/share/linux-monitor"
    python3 -m venv "$dest"
    "$dest/bin/pip" install --quiet --upgrade .
    mkdir -p "$HOME/.local/bin"
    ln -sf "$dest/bin/lmon" "$HOME/.local/bin/lmon"
    bin="$HOME/.local/bin/lmon"
fi
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Add ~/.local/bin to PATH:  echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.bashrc && . ~/.bashrc" ;;
esac

# `sudo lmon`: sudo only searches system directories, so link the command into /usr/local/bin (asks for your password once)
if [ "$(readlink -f /usr/local/bin/lmon 2>/dev/null)" != "$(readlink -f "$bin")" ]; then
    if [ -t 0 ]; then
        printf "Also enable 'sudo lmon' (links %s into /usr/local/bin)? [Y/n] " "$bin"
        read -r answer
        case "$answer" in
            n|N|no) echo "Skipped. Later: sudo ln -sf $bin /usr/local/bin/lmon" ;;
            *) sudo ln -sf "$bin" /usr/local/bin/lmon && echo "sudo lmon is ready." ;;
        esac
    else
        echo "To enable 'sudo lmon': sudo ln -sf $bin /usr/local/bin/lmon"
    fi
fi
echo "Installed. Run:  lmon --demo (a made-up machine: safe to look around and take screenshots)"
echo "                 lmon        (your machine, Ctrl+C to exit)"
echo "                 sudo lmon   (also firewall rules and every port's owner)"
echo "                 lmon --web  (web dashboard on this machine, port \${MONITOR_PORT:-8202})"
