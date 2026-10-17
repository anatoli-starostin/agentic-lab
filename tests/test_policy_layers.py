"""Policy layering: base -> role -> overlay, strict schema, removals, role differences."""
import pytest

from agentic_lab import policy
from agentic_lab.policy import model


def b(p, cmd):
    return policy.classify(p, "Bash", {"command": cmd}).decision


def test_builtin_roles_load():
    for role in policy.ROLES:
        p = policy.load(role)
        assert p.layers == ("base", f"role:{role}")


def test_unknown_role():
    with pytest.raises(policy.PolicyError, match="unknown role"):
        policy.load("admin")


def test_base_alone_is_read_only():
    p = policy.load()
    assert b(p, "ls") == "green"
    assert b(p, "sbox python x") == "gated"           # no cage in base
    assert b(p, "git status") == "gated"              # no git in base
    assert policy.classify(p, "Write", {"file_path": "/tmp/x"}).decision == "gated"


def test_role_differences():
    w, m, s = (policy.load(r) for r in ("worker", "manager", "secretary"))
    # web reads: manager only
    assert policy.classify(m, "WebFetch", {}).decision == "green"
    assert policy.classify(w, "WebFetch", {}).decision == "gated"
    assert policy.classify(s, "WebFetch", {}).decision == "gated"
    # GitHub writes: never green for the manager
    assert b(m, "git push origin x") == "gated"
    assert b(m, "git commit -m x") == "gated"
    assert b(m, "git log") == "green"
    assert b(w, "git push origin x") == "green"
    # the cage: worker gets tailnet, manager doesn't
    assert b(w, "sbox --net tailnet -- x") == "green"
    assert b(m, "sbox --net tailnet -- x") == "gated"
    assert b(m, "sbox python x") == "green"
    assert b(s, "sbox python x") == "gated"


def test_overlay_extends_and_removes(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("bash:\n  safe_patterns: ['^lab-status$']\n"
                 "remove:\n  bash:\n    git:\n      subcommands: [push]\n")
    p = policy.load("worker", [f])
    assert b(p, "lab-status") == "green"
    assert b(p, "git push origin x") == "gated"
    assert b(p, "git commit -m x") == "green"
    assert p.layers[-1] == str(f)


def test_overlay_scalar_override(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("bash:\n  sbox:\n    enabled: false\n")
    assert b(policy.load("worker", [f]), "sbox python x") == "gated"


@pytest.mark.parametrize("text,msg", [
    ("bash:\n  safe_patern: []\n", "unknown key 'safe_patern'"),
    ("writable: ~/x\n", "expected list"),
    ("bash:\n  safe_patterns: ['(']\n", "bad regex"),
    ("bash:\n  helpers: [{path: relative/x}]\n", "absolute path"),
    ("bash:\n  helpers: [{path: /x, run_as: root}]\n", "bad entry"),
    ("remove:\n  bash:\n    sbox:\n      enabled: true\n", "only list items"),
])
def test_strict_schema(tmp_path, text, msg):
    f = tmp_path / "o.yaml"
    f.write_text(text)
    with pytest.raises(policy.PolicyError, match=msg):
        policy.load("worker", [f])


def test_merge_dedups_lists():
    raw = model.merge_layers([("a", {"green_tools": ["X", "Y"]}), ("b", {"green_tools": ["Y", "Z"]})])
    assert raw["green_tools"] == ["X", "Y", "Z"]


def test_tool_patterns(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("green_tools: ['mcp__wandb__get_*']\n")
    p = policy.load("manager", [f])
    assert policy.classify(p, "mcp__wandb__get_run", {}).decision == "green"
    assert policy.classify(p, "mcp__wandb__delete_run", {}).decision == "gated"
