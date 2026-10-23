"""A member's own keys for outside services (W&B, Hugging Face, an S3 bucket...), as
environment variables for all of that member's dedicated agents.

The member gives a key to the secretary ("store my W&B key as WANDB_API_KEY"); its value
arrives through the intake (a one-time link, never a chat) and is kept with the lab's other
secrets, under `humans/<member>/env/<NAME>`. `lab apply` installs a member's keys for each
of their **dedicated** workers and managers (`access: dedicated`, the default): never a
shared agent, whose tasks are other members' too. They go into the agent's Claude Code
sessions and its long runs as environment variables (`member-env.json` in the service's
state folder, which the agent's sandbox can't read: it sees the variables, not the file).

Unlike the lab's own secrets, these are meant to be used by the agent, so its commands can
read them. What keeps them in check: only the member's own agents get them, sandboxed
commands reach only allowed sites (a key gives no network access: that's a grant), and the
member should give the lab a key of its own (revocable, scoped), not a personal one.

Delivery is a deploy: each host records the revision of the keys it got (`member_env` in
applied.yaml), and the secretary's catch-up (#61) redeploys a host whose revision differs,
now or when it next starts.
"""
import hashlib
import json
import os
import re

NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PREFIX = "env:"                 # the intake's secret name for a member key: env:<NAME>
FILE = "member-env.json"        # in the agent's state folder (~/.local/state/agentic-lab)
# set by the lab itself, or able to change how programs run: never a member's key
RESERVED = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "LANG", "TERM",
            "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "PERL5LIB",
            "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
            "UV_CACHE_DIR", "PIP_CACHE_DIR", "HF_HOME", "TORCH_EXTENSIONS_DIR",
            "TRITON_CACHE_DIR", "CUDA_CACHE_PATH", "CUDA_VISIBLE_DEVICES",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
            "ENABLE_TOOL_SEARCH", "ANTHROPIC_API_KEY"}
RESERVED_PREFIXES = ("LAB_", "CLAUDE_", "ANTHROPIC_", "GIT_", "GH_", "SSH_", "SUDO_", "LD_",
                     "BASH_", "SYSTEMD_", "SLACK_")


class MemberEnvError(ValueError):
    pass


def check_name(name):
    """The variable's name, or MemberEnvError saying why it can't be one."""
    n = (name or "").strip()
    if not NAME.match(n):
        raise MemberEnvError(f"{name!r} isn't a valid variable name: capital letters, digits "
                             "and _, starting with a letter (e.g. WANDB_API_KEY)")
    if n in RESERVED or n.startswith(RESERVED_PREFIXES):
        raise MemberEnvError(f"{n} is reserved: the lab sets it itself or it changes how "
                             "programs run (names starting with "
                             f"{', '.join(RESERVED_PREFIXES)} are too). Pick another name.")
    return n


def receives(lab, aid):
    """Does agent `aid` get its member's keys? Dedicated workers and managers only."""
    a = lab.agents.get(aid) or {}
    return a.get("role") in ("worker", "manager") and a.get("access", "dedicated") == "dedicated"


def agents_of(lab, human):
    """The agents a member's keys go to."""
    return [aid for aid, a in lab.agents.items() if a.get("human") == human and receives(lab, aid)]


def for_agent(lab, aid, load):
    """{NAME: value} for agent `aid`; `load(human)` -> that member's keys."""
    return dict(load(lab.agents[aid]["human"]) or {}) if receives(lab, aid) else {}


def revision(lab, host, load):
    """A short hash of the keys `host`'s agents get (names and values), or None if none:
    what applied.yaml records, so a host with other keys is caught up."""
    items = sorted((aid, k, hashlib.sha256(v.encode()).hexdigest())
                   for aid, a in lab.agents.items() if a.get("host") == host
                   for k, v in for_agent(lab, aid, load).items())
    if not items:
        return None
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


def load_dir(secrets_root, human):
    """A member's keys as the intake stored them: {NAME: value}."""
    d = os.path.join(secrets_root, "humans", human, "env")
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if n.startswith(".") or n.endswith(".meta") or not NAME.match(n):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                out[n] = f.read().strip()
        except OSError:
            continue
    return out


def read(state_dir):
    """The keys installed for this agent (its service and its runs read them): {} if none."""
    try:
        with open(os.path.join(os.path.expanduser(state_dir), FILE)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in (d or {}).items() if isinstance(k, str) and NAME.match(k)
            and not (k in RESERVED or k.startswith(RESERVED_PREFIXES))}
