"""Policy as configuration: the data model, layered loading and validation.

A policy is built from layers, each a YAML mapping with the same shape:

    base.yaml               shipped with the framework (safe defaults)
    roles/<role>.yaml       role defaults (worker / manager / secretary)
    <deploy overlay>.yaml   per-agent overlay named in lab.yaml

Merging: mappings merge recursively, lists are extended (deduplicated, order kept),
scalars are overridden. A layer may also carry a top-level `remove:` mapping with the
same shape, whose list items are removed from what came before. Unknown keys are an
error, so a typo can never silently widen or narrow a policy.
"""
import copy
import os
import re
from dataclasses import dataclass, field
from importlib import resources

import yaml


class PolicyError(ValueError):
    pass


# The schema: every key a layer may set, with its type. Nested dicts are sub-schemas.
_SCHEMA = {
    "ungated_tools": list,      # read-only tools deferred to Claude Code (fnmatch patterns)
    "green_tools": list,        # tools auto-allowed outright (fnmatch patterns)
    "file_tools": list,         # tools whose `file_path` must be inside `writable`
    "writable": list,           # the writable green zone (mirrors the cage's binds)
    "bash": {
        "safe_patterns": list,  # regexes; a segment matching one is read-only safe
        "sbox": {"enabled": bool, "net_modes": list},
        "git": {"enabled": bool, "subcommands": list},
        "helpers": list,        # trusted executables by absolute path (see Helper)
    },
    "monitor_commands": list,   # exact Monitor commands auto-allowed
}
_HELPER_KEYS = {"path", "subcommands", "allow_quoted_heredoc"}


@dataclass(frozen=True)
class Helper:
    """A vetted executable trusted by ABSOLUTE path. It must live outside the writable
    zone, or the governed agent could rewrite it and still have it auto-run."""
    path: str
    subcommands: tuple | None = None    # None = any; else the first arg must be one of these
    allow_quoted_heredoc: bool = False  # `helper sub ID <<'EOF' ... EOF` (literal data)


@dataclass(frozen=True)
class Policy:
    ungated_tools: tuple = ()
    green_tools: tuple = ()
    file_tools: tuple = ()
    writable: tuple = ()
    safe_patterns: tuple = ()
    sbox_enabled: bool = False
    sbox_net_modes: tuple = ()
    git_enabled: bool = False
    git_subcommands: frozenset = frozenset()
    helpers: tuple = ()
    monitor_commands: tuple = ()
    layers: tuple = field(default=(), compare=False)   # where it came from, for `explain`


def _check(layer, schema, where):
    if not isinstance(layer, dict):
        raise PolicyError(f"{where}: expected a mapping, got {type(layer).__name__}")
    for k, v in layer.items():
        if k not in schema:
            raise PolicyError(f"{where}: unknown key {k!r}")
        want = schema[k]
        if isinstance(want, dict):
            _check(v, want, f"{where}.{k}")
        elif not isinstance(v, want):
            raise PolicyError(f"{where}.{k}: expected {want.__name__}, got {type(v).__name__}")


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict):
            _merge(dst.setdefault(k, {}), v)
        elif isinstance(v, list):
            cur = dst.setdefault(k, [])
            for item in v:
                if item not in cur:
                    cur.append(item)
        else:
            dst[k] = v


def _remove(dst, rm):
    for k, v in rm.items():
        if k not in dst:
            continue
        if isinstance(v, dict):
            _remove(dst[k], v)
        elif isinstance(v, list):
            dst[k] = [x for x in dst[k] if x not in v]
        else:
            raise PolicyError(f"remove.{k}: only list items can be removed")


def merge_layers(layers):
    """layers: list of (name, mapping). Returns the merged raw mapping."""
    out = {}
    for name, layer in layers:
        layer = copy.deepcopy(layer or {})
        rm = layer.pop("remove", None)
        _check(layer, _SCHEMA, name)
        if rm is not None:
            _check(rm, _SCHEMA, f"{name}.remove")
            _remove(out, rm)
        _merge(out, layer)
    return out


def _expand(p):
    return os.path.realpath(os.path.expanduser(p))


def build(raw, layer_names=()):
    """Raw merged mapping -> validated Policy."""
    bash = raw.get("bash", {})
    sbox = bash.get("sbox", {})
    git = bash.get("git", {})
    writable = tuple(_expand(p) for p in raw.get("writable", []))
    for pat in bash.get("safe_patterns", []):
        try:
            re.compile(pat)
        except re.error as e:
            raise PolicyError(f"bash.safe_patterns: bad regex {pat!r}: {e}") from None
    helpers = []
    for h in bash.get("helpers", []):
        if not isinstance(h, dict) or "path" not in h or set(h) - _HELPER_KEYS:
            raise PolicyError(f"bash.helpers: bad entry {h!r} (keys: {sorted(_HELPER_KEYS)})")
        path = os.path.expanduser(h["path"])
        if not os.path.isabs(path):
            raise PolicyError(f"bash.helpers: {h['path']!r} must be an absolute path")
        if _in(writable, os.path.realpath(path)):
            raise PolicyError(f"bash.helpers: {h['path']!r} is inside the writable zone; "
                              "a trusted helper must live where the agent cannot rewrite it")
        subs = h.get("subcommands")
        helpers.append(Helper(path, tuple(subs) if subs is not None else None,
                              bool(h.get("allow_quoted_heredoc", False))))
    return Policy(
        ungated_tools=tuple(raw.get("ungated_tools", [])),
        green_tools=tuple(raw.get("green_tools", [])),
        file_tools=tuple(raw.get("file_tools", [])),
        writable=writable,
        safe_patterns=tuple(bash.get("safe_patterns", [])),
        sbox_enabled=bool(sbox.get("enabled", False)),
        sbox_net_modes=tuple(sbox.get("net_modes", [])),
        git_enabled=bool(git.get("enabled", False)),
        git_subcommands=frozenset(git.get("subcommands", [])),
        helpers=tuple(helpers),
        monitor_commands=tuple(raw.get("monitor_commands", [])),
        layers=tuple(layer_names),
    )


def _in(roots, rp):
    return any(rp == b or rp.startswith(b + os.sep) for b in roots)


def _builtin(name):
    text = resources.files("agentic_lab.policy").joinpath(name).read_text()
    return yaml.safe_load(text) or {}


ROLES = ("worker", "manager", "secretary")


def load(role=None, overlays=()):
    """Base + optional role defaults + overlay files (paths) -> Policy."""
    layers = [("base", _builtin("base.yaml"))]
    if role is not None:
        if role not in ROLES:
            raise PolicyError(f"unknown role {role!r} (expected one of {ROLES})")
        layers.append((f"role:{role}", _builtin(f"roles/{role}.yaml")))
    for path in overlays:
        with open(path) as f:
            layers.append((str(path), yaml.safe_load(f) or {}))
    return build(merge_layers(layers), [n for n, _ in layers])
