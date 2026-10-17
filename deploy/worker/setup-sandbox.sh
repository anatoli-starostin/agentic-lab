#!/bin/bash
# Prepare a worker host for Claude Code's built-in sandbox (DESIGN.md §6.1).
# Run as root on a fresh Ubuntu 24.04+ VM:  sudo ./setup-sandbox.sh
#
#   1. packages the sandbox needs: bubblewrap, socat
#   2. AppArmor: a bwrap profile that lets the sandbox's seccomp helper work
#   3. bwrap-gpu: a wrapper that passes the GPU into the sandbox
#   4. managed settings pointing Claude Code at the wrapper (agents can't change them)
#
# Then create the agent's user with create-agent-user.sh.
#
# The agent's own sandbox settings (allowlist, writable dirs, hidden /run, Unix sockets for
# CUDA) are generated per agent by `lab settings <agent>`, on this host.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

echo "== packages"
apt-get install -y bubblewrap socat >/dev/null
apt-get install -y gh >/dev/null 2>&1 || echo "   (gh not installed: agents can still use git and the GitHub API)"

echo "== apparmor"
if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ]; then
  # Ubuntu's restrictive bwrap profile (if loaded) would conflict: both attach to /usr/bin/bwrap
  if [ -e /etc/apparmor.d/bwrap-userns-restrict ]; then
    ln -sf /etc/apparmor.d/bwrap-userns-restrict /etc/apparmor.d/disable/bwrap-userns-restrict
    apparmor_parser -R /etc/apparmor.d/bwrap-userns-restrict 2>/dev/null || true
  fi
  install -m 644 "$here/apparmor-bwrap" /etc/apparmor.d/bwrap
  apparmor_parser -r /etc/apparmor.d/bwrap
  echo "   loaded /etc/apparmor.d/bwrap"
else
  echo "   unprivileged userns not restricted; nothing to do"
fi

echo "== bwrap-gpu"
install -d /usr/local/libexec/agentic-lab
install -m 755 "$here/bwrap-gpu" /usr/local/libexec/agentic-lab/bwrap-gpu

echo "== managed settings"
install -d /etc/claude-code
settings=/etc/claude-code/managed-settings.json
if [ -e "$settings" ]; then
  # keep whatever else is there; make sure the sandbox uses bwrap-gpu
  python3 - "$settings" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
want = "/usr/local/libexec/agentic-lab/bwrap-gpu"
if d.get("sandbox", {}).get("bwrapPath") == want:
    print("   already set")
else:
    d.setdefault("sandbox", {})["bwrapPath"] = want
    json.dump(d, open(p, "w"), indent=2)
    print("   added bwrapPath")
PY
else
  cat > "$settings" <<JSON
{
  "sandbox": {
    "bwrapPath": "/usr/local/libexec/agentic-lab/bwrap-gpu"
  }
}
JSON
  chmod 644 "$settings"
  echo "   wrote $settings"
fi
echo "done"
