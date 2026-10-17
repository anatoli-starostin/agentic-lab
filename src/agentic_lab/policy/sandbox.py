"""Each agent's Claude Code settings, generated from lab.yaml (DESIGN.md §6.1).

One model for every agent:

    inside the sandbox (auto-allowed): no internet, tailnet, working folder + tmp, GPU
    outside the sandbox (ask a human): internet, writes; reads are free

Enforcement is Claude Code's built-in sandbox. This module only renders the settings: the
sandbox locked on, the network allowlist (tailnet + domain grants) and the writable folders
(working folder, tmp + write grants), plus permission rules for tool grants. The agent
service passes them to the Agent SDK (`ClaudeAgentOptions(sandbox=..., settings=...)`) or
writes them as managed settings, so the agent can't change them.

Verified on a cloud GPU worker (Ubuntu 24.04, H100, Claude Code 2.1.302), 2026-10-17:
docs/security-notes.md has the details.
"""
import os

from . import grants as G

TMP = "/tmp"

# /run holds the host's Unix sockets: the user's systemd manager (`systemd-run --user` would
# start a process OUTSIDE the sandbox), D-Bus, snapd, tailscaled... The sandbox hides all of
# it. The only socket an agent needs there is nvidia-persistenced's, which CUDA opens in
# cuInit.
HIDDEN = ["/run", "/var/run"]
GPU_VISIBLE = ["/run/nvidia-persistenced"]


def _abs(path):
    return os.path.normpath(os.path.expanduser(path))


def _rule_path(path):
    """A path for a permission rule: `//abs/path` is absolute in Claude Code's rule syntax."""
    return "/" + _abs(path)


def settings(workdir, tailnet=None, trust_tailnet=True, grant_list=(), now=None, gpu=False):
    """Render the Claude Code settings for one agent.

    workdir:       the agent's working folder (writable inside the sandbox)
    tailnet:       the tailnet's DNS suffix (e.g. example.ts.net)
    trust_tailnet: tailnet resources count as safe -> reachable from inside the sandbox
    grant_list:    standing and timed Grants; expired ones are dropped (needs `now`)
    gpu:           the agent runs CUDA inside the sandbox (GPU workers)

    CUDA opens a Unix socket in cuInit, and Claude Code's seccomp filter can only block
    Unix sockets wholesale. So a GPU agent allows Unix sockets, and the sandbox hides /run
    (where the host's sockets live) instead. Other agents keep the seccomp block as well.
    The GPU devices themselves come from the host setup (deploy/worker/bwrap-gpu).
    """
    live = G.active(grant_list, now) if now is not None else \
        [g for g in grant_list if g.expires is None]
    domains, writes, tools = [], [], []
    if trust_tailnet and tailnet:
        domains.append(f"*.{tailnet}")
    for g in live:
        {"domain": domains, "write": writes, "tool": tools}[g.kind].append(g.value)
    writable = [_abs(workdir), TMP] + [_abs(w) for w in writes]
    # Tools keep their caches (pip, uv, Hugging Face, torch hub...) under XDG_CACHE_HOME.
    # ~/.cache isn't writable in the sandbox, so point it into the working folder: caches
    # then persist between tasks without widening the sandbox.
    cache = os.path.join(_abs(workdir), ".cache")

    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,          # never silently run unsandboxed
            "autoAllowBashIfSandboxed": True,   # inside the sandbox: no prompts
            # The agent may retry a refused command outside the sandbox. That retry is a
            # permission request (never auto-allowed), which goes to the broker and a human.
            "allowUnsandboxedCommands": True,
            "network": {
                "allowedDomains": _dedup(domains),
                "strictAllowlist": True,        # refuse unknown hosts instead of prompting
                "allowAllUnixSockets": gpu,     # CUDA needs Unix sockets (see above)
            },
            "filesystem": {
                "allowWrite": _dedup(writable),
                "denyRead": list(HIDDEN),
                "allowRead": list(GPU_VISIBLE) if gpu else [],
            },
        },
        "env": {"XDG_CACHE_HOME": cache},
        "permissions": {
            "defaultMode": "default",           # anything not allowed -> permission request
            "allow": _dedup(tools + [f"Edit({_rule_path(w)}/**)" for w in writable]),
        },
    }


def _dedup(xs):
    return list(dict.fromkeys(xs))
