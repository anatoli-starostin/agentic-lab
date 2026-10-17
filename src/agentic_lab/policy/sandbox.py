"""Each agent's Claude Code settings, generated from lab.yaml (DESIGN.md §6.1).

One model for every agent:

    inside the sandbox (auto-allowed): no internet, tailnet, working folder + tmp, GPU
    outside the sandbox (ask a human): internet, writes; reads are free

Enforcement is Claude Code's built-in sandbox. This module only renders the settings: the
sandbox locked on, the network allowlist (tailnet + domain grants) and the writable folders
(working folder, tmp + write grants), plus permission rules for tool grants. The agent
service passes them to the Agent SDK (`ClaudeAgentOptions(sandbox=..., settings=...)`) or
writes them as managed settings, so the agent can't change them.

Keys marked VERIFY are taken from the docs but not yet exercised on a real host (M1).
"""
import os

from . import grants as G

TMP = "/tmp"


def _abs(path):
    return os.path.normpath(os.path.expanduser(path))


def _rule_path(path):
    """A path for a permission rule: `//abs/path` is absolute in Claude Code's rule syntax."""
    return "/" + _abs(path)


def settings(workdir, tailnet=None, trust_tailnet=True, grant_list=(), now=None):
    """Render the Claude Code settings for one agent.

    workdir:       the agent's working folder (writable inside the sandbox)
    tailnet:       the tailnet's DNS suffix (e.g. example.ts.net)
    trust_tailnet: tailnet resources count as safe -> reachable from inside the sandbox
    grant_list:    standing and timed Grants; expired ones are dropped (needs `now`)
    """
    live = G.active(grant_list, now) if now is not None else \
        [g for g in grant_list if g.expires is None]
    domains, writes, tools = [], [], []
    if trust_tailnet and tailnet:
        domains.append(f"*.{tailnet}")
    for g in live:
        {"domain": domains, "write": writes, "tool": tools}[g.kind].append(g.value)
    writable = [_abs(workdir), TMP] + [_abs(w) for w in writes]

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
                "strictAllowlist": True,        # VERIFY: refuse unknown hosts, don't prompt
            },
            "filesystem": {
                "allowWrite": _dedup(writable),  # VERIFY: key name for extra writable paths
            },
        },
        "permissions": {
            "defaultMode": "default",           # anything not allowed -> permission request
            "allow": _dedup(tools + [f"Edit({_rule_path(w)}/**)" for w in writable]),
        },
    }


def _dedup(xs):
    return list(dict.fromkeys(xs))
