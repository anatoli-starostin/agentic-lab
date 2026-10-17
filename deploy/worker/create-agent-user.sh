#!/bin/bash
# Create the dedicated Unix user an agent runs as (DESIGN.md §10).
# Run as root:  sudo ./create-agent-user.sh [user]      (default: lab-agent)
#
# Nobody ever logs in as this user, and nothing but the agent's service runs as it. GPU
# agents allow Unix sockets (CUDA needs one), so any socket server running as this user
# (tmux, a forwarded SSH agent, Jupyter, ...) would be a way out of the sandbox.
#
#   - system user, shell nologin, password locked, no SSH keys
#   - lingering off: no per-user systemd manager
#   - home with the working folder ~/work, private to the user
set -euo pipefail
user=${1:-lab-agent}

if id "$user" >/dev/null 2>&1; then
  echo "user $user exists"
else
  useradd --system --create-home --home-dir "/home/$user" --shell /usr/sbin/nologin "$user"
  echo "created user $user"
fi
passwd -l "$user" >/dev/null
loginctl disable-linger "$user" 2>/dev/null || true

home=$(getent passwd "$user" | cut -d: -f6)
rm -rf "$home/.ssh"
install -d -m 700 -o "$user" -g "$user" "$home" "$home/work"

# GPU access: the device nodes are world-accessible on most hosts; if this host restricts
# them to a group (e.g. video, render), add the user to it.
for g in video render; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$user"
done

echo "linger: $(loginctl show-user "$user" -p Linger 2>/dev/null || echo 'Linger=no (no session)')"
echo "home:   $home (work folder: $home/work)"
