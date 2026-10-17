#!/bin/bash
# Store a secret for an agent, typed hidden, so it never appears in a chat, a log or the
# shell history.  Run as root:  sudo ./set-secret.sh lab-<agent-id> <name>
#
#   claude-oauth-token     the Claude seat token (from `claude setup-token`)
#   manager-tokens.yaml    for a worker: `<manager-id>: <token>` lines (multi-line: end with Ctrl-D)
set -euo pipefail
user=${1:?usage: set-secret.sh lab-<agent-id> <name>}
name=${2:?usage: set-secret.sh lab-<agent-id> <name>}
case "$name" in
  claude-oauth-token|manager-tokens.yaml) ;;
  *) echo "unknown secret $name" >&2; exit 2 ;;
esac
home=$(getent passwd "$user" | cut -d: -f6)
[ -n "$home" ] || { echo "no user $user" >&2; exit 2; }
dir="$home/.config/agentic-lab"
install -d -m 700 -o "$user" -g "$user" "$home/.config" "$dir"

if [ "$name" = "manager-tokens.yaml" ]; then
  echo "Type or paste '<manager-id>: <token>' lines, then Ctrl-D:"
  value=$(cat)
else
  read -r -s -p "Paste $name (input hidden), then Enter: " value; echo
fi
[ -n "$value" ] || { echo "empty; nothing stored" >&2; exit 1; }
umask 077
printf '%s\n' "$value" > "$dir/$name.tmp"
chown "$user:$user" "$dir/$name.tmp"
chmod 600 "$dir/$name.tmp"
mv "$dir/$name.tmp" "$dir/$name"
echo "stored $dir/$name (owner $user, mode 600)"
