"""A member's own keys for outside services (member_env.py): through the intake, to all of
their dedicated agents, delivered by a deploy that the catch-up starts when a host runs
other keys than it should."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import apply as A
from agentic_lab import member_env as ME
from agentic_lab import registry
from agentic_lab.secrets import intake as I
from agentic_lab.secretary import catchup as CU
from agentic_lab.secretary.service import Secretary

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
NEW, FW = "c" * 40, "f" * 40
TOKENS = SimpleNamespace(get=lambda k: "t")


def lab():
    """alice: mgr-alice (manager), worker-b (dedicated); worker-a is shared. bob: none."""
    lb = registry.load(EXAMPLE)
    lb.humans["bob"] = {"name": "Bob", "slack_user": "U-BOB"}
    return lb


# ── names ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["WANDB_API_KEY", "HF_TOKEN", "AWS_ACCESS_KEY_ID", "X1"])
def test_good_names(name):
    assert ME.check_name(f" {name} ") == name


@pytest.mark.parametrize("name", ["wandb_api_key", "1KEY", "MY-KEY", "", "A" * 65,
                                  "PATH", "LD_PRELOAD", "CLAUDE_CODE_OAUTH_TOKEN",
                                  "ANTHROPIC_BASE_URL", "LAB_ANYTHING", "GIT_CONFIG_GLOBAL",
                                  "HF_HOME", "PYTHONPATH", "HTTPS_PROXY", "SLACK_BOT_TOKEN"])
def test_bad_or_reserved_names_are_refused(name):
    with pytest.raises(ME.MemberEnvError):
        ME.check_name(name)


# ── the intake ───────────────────────────────────────────────────────────────

def test_the_intake_takes_a_members_key_and_never_shows_it(tmp_path):
    it = I.Intake(tmp_path)
    code = it.request("alice", "env:WANDB_API_KEY")
    assert I.fields("env:WANDB_API_KEY")[0][1].startswith("WANDB_API_KEY")
    assert it.receive(code, "env:WANDB_API_KEY", "  wandb-secret-value  ") == "alice"
    stored = tmp_path / "secrets" / "humans" / "alice" / "env" / "WANDB_API_KEY"
    assert stored.read_text() == "wandb-secret-value\n"
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    assert it.member_env("alice") == {"WANDB_API_KEY": "wandb-secret-value"}
    info = it.member_env_info("alice")
    assert [n for n, _ in info] == ["WANDB_API_KEY"] and "wandb-secret-value" not in str(info)
    assert it.delete_member_env("alice", "WANDB_API_KEY") is True
    assert it.member_env("alice") == {} and not it.delete_member_env("alice", "WANDB_API_KEY")


def test_the_intake_refuses_bad_names_service_owners_and_multiline_values(tmp_path):
    it = I.Intake(tmp_path)
    for bad in ("env:PATH", "env:lower", "env:"):
        with pytest.raises(I.IntakeError):
            it.request("alice", bad)
    with pytest.raises(I.IntakeError):
        it.request("service:broker", "env:WANDB_API_KEY")
    code = it.request("alice", "env:HF_TOKEN")
    for bad in ("", "two\nlines", "x" * 9000):
        with pytest.raises(I.IntakeError):
            it.receive(code, "env:HF_TOKEN", bad)
    assert it.receive(code, "env:HF_TOKEN", "hf_x") == "alice"     # still usable after errors


# ── lab apply ────────────────────────────────────────────────────────────────

def keys(store):
    return lambda human: dict(store.get(human, {}))


def plan(lb, host, store):
    return A.host_plan(lb, host, TOKENS, intake=lambda *a: "x\n", member_env=keys(store))


def installed(p, user):
    path = f"/home/{user}/.local/state/agentic-lab/{ME.FILE}"
    return json.loads(p["files"][path][2])


def test_a_members_keys_go_to_all_their_dedicated_agents_only():
    lb, store = lab(), {"alice": {"WANDB_API_KEY": "w1"}}
    mgr, ded, shared = (lb.agents[a]["host"] for a in ("mgr-alice", "worker-b", "worker-a"))
    assert installed(plan(lb, mgr, store), lb.unix_user("mgr-alice")) == {"WANDB_API_KEY": "w1"}
    assert installed(plan(lb, ded, store), lb.unix_user("worker-b")) == {"WANDB_API_KEY": "w1"}
    assert installed(plan(lb, shared, store), lb.unix_user("worker-a")) == {}   # shared: never
    lb.agents["worker-b"]["human"] = "bob"                          # someone else's agent
    assert installed(plan(lb, ded, store), lb.unix_user("worker-b")) == {}


def test_the_revision_follows_names_and_values_and_is_none_without_keys():
    lb = lab()
    host = lb.agents["worker-b"]["host"]
    r1 = plan(lb, host, {"alice": {"WANDB_API_KEY": "w1"}})["member_env"]
    r2 = plan(lb, host, {"alice": {"WANDB_API_KEY": "w2"}})["member_env"]
    r3 = plan(lb, host, {"alice": {"WANDB_API_KEY": "w1", "HF_TOKEN": "h"}})["member_env"]
    assert r1 and len({r1, r2, r3}) == 3
    assert r1 == plan(lb, host, {"alice": {"WANDB_API_KEY": "w1"}})["member_env"]
    p = plan(lb, host, {})
    assert p["member_env"] is None
    assert installed(p, lb.unix_user("worker-b")) == {}    # written anyway: removals reach it


def test_a_service_reads_only_valid_unreserved_names(tmp_path):
    (tmp_path / ME.FILE).write_text(json.dumps(
        {"WANDB_API_KEY": "w", "PATH": "/evil", "LD_PRELOAD": "x", "bad-name": "y"}))
    assert ME.read(tmp_path) == {"WANDB_API_KEY": "w"}
    assert ME.read(tmp_path / "none") == {}


# ── the catch-up ─────────────────────────────────────────────────────────────

def state(rev):
    """The secretary's host runs NEW; worker-b's host reports NEW with keys `rev`."""
    ap = {"config_commit": NEW, "framework_commit": FW, **({"member_env": rev} if rev else {})}
    return {"applied": {"config_commit": NEW, "framework_commit": FW},
            "agents": {"worker-b": {"up": True, "host": "gpu-b", "applied": ap}}}


def test_a_host_on_the_labs_commit_with_other_keys_is_caught_up():
    want = {"gpu-b": "rev-2"}
    assert set(CU.behind(state("rev-1"), "nuc-01", None, want)) == {"gpu-b"}
    assert set(CU.behind(state(None), "nuc-01", None, want)) == {"gpu-b"}   # never had any
    assert CU.behind(state("rev-2"), "nuc-01", None, want) == {}
    assert CU.behind(state(None), "nuc-01", None, {"gpu-b": None}) == {}
    assert CU.behind(state("rev-1"), "nuc-01", None) == {}      # keys not given: commits only


# ── the secretary ────────────────────────────────────────────────────────────

def secretary(tmp_path):
    sec = Secretary.__new__(Secretary)
    sec.lab, sec.slack, sec.events = lab(), None, None
    sec.lab.humans["alice"]["admin"] = True
    sec.intake = I.Intake(tmp_path)
    return sec


def test_the_secretary_lists_names_only_and_who_gets_them(tmp_path):
    sec = secretary(tmp_path)
    sec.intake.store("alice", "env:WANDB_API_KEY", "w-secret")
    out = asyncio.run(sec.member_secret("list", None, "alice", asked_by="alice"))
    assert "WANDB_API_KEY" in out and "w-secret" not in out
    assert "Flash" in out and "worker-b" in out and "worker-a" not in out


def test_the_secretary_refuses_bad_names_and_other_members_keys(tmp_path):
    sec = secretary(tmp_path)
    out = asyncio.run(sec.member_secret("set", "PATH", "alice", asked_by="alice"))
    assert out.startswith("refused") and "reserved" in out
    out = asyncio.run(sec.member_secret("set", "wandb", "alice", asked_by="alice"))
    assert out.startswith("refused") and "valid variable name" in out
    sec.intake.store("alice", "env:WANDB_API_KEY", "w")
    out = asyncio.run(sec.member_secret("delete", "WANDB_API_KEY", "alice", asked_by="bob"))
    assert out.startswith("refused") and sec.intake.member_env("alice")     # kept
    out = asyncio.run(sec.member_secret("delete", "WANDB_API_KEY", "alice", asked_by="alice"))
    assert out.startswith("removed") and sec.intake.member_env("alice") == {}


def test_a_key_for_a_member_without_dedicated_agents_is_refused(tmp_path):
    sec = secretary(tmp_path)
    out = asyncio.run(sec.member_secret("set", "WANDB_API_KEY", "bob", asked_by="bob"))
    assert "no dedicated agents" in out


def test_a_catch_up_deploys_a_host_on_the_same_commit_with_other_keys(monkeypatch, capsys):
    """apply-main --catch-up: same commits but other member keys is a deploy, not 'up to
    date'; same keys is up to date; the applied record carries the keys' revision."""
    from agentic_lab import cli, lock
    from agentic_lab import configrepo as CR
    lb = registry.load(EXAMPLE)
    plans = [{"host": "gpu-a", "name": "a", "users": ["u"], "units": [], "files": {},
              "envs": {}, "missing": [], "member_env": "rev-2"},
             {"host": "gpu-b", "name": "b", "users": ["u"], "units": [], "files": {},
              "envs": {}, "missing": [], "member_env": "rev-2"}]
    ran = {}
    monkeypatch.setattr(A, "plan_all", lambda path, hosts=None: (lb, SimpleNamespace(
        save=lambda: None), plans))
    monkeypatch.setattr(registry, "check", lambda lab: [])
    monkeypatch.setattr(registry, "load", lambda path: lb)
    monkeypatch.setattr(lock, "current", lambda src: (None, FW))
    monkeypatch.setattr(cli, "deploy_lock", lambda: None)
    now = {"gpu-a": {"config_commit": NEW, "framework_commit": FW, "member_env": "rev-1"},
           "gpu-b": {"config_commit": NEW, "framework_commit": FW, "member_env": "rev-2"}}
    monkeypatch.setattr(A, "applied_on", lambda p, local, admin: now[p["host"]])
    monkeypatch.setattr(A, "run_bundle", lambda p, local, admin: ran.setdefault(
        p["host"], p["applied"]) and 0)
    rc = cli.deploy("lab.yaml", {"config_commit": NEW}, ".", "admin",
                    only_behind=lambda old: False)
    out = capsys.readouterr().out
    assert rc == 0 and set(ran) == {"gpu-a"}
    assert ran["gpu-a"]["member_env"] == "rev-2"
    assert "CATCH-UP gpu-b current" in out and "CATCH-UP gpu-a done" in out


def test_a_dedicated_agent_on_the_secretarys_host_is_named_as_needing_a_deploy(tmp_path):
    sec = secretary(tmp_path)
    sec.lab.agents["mgr-alice"]["host"] = "nuc-01"          # the secretary's host
    sec.intake.store("alice", "env:WANDB_API_KEY", "w")
    out = asyncio.run(sec.member_secret("delete", "WANDB_API_KEY", "alice", asked_by="alice"))
    assert "Flash run(s) on my own machine" in out and "offer_deploy" in out
