"""Generated sandbox settings and grants (DESIGN.md §6)."""
import datetime as dt
import os

import pytest

from agentic_lab.policy import grants as G
from agentic_lab.policy import sandbox

NOW = dt.datetime(2026, 10, 17, 12, 0)
HOME = os.path.expanduser("~")


def test_base_model():
    s = sandbox.settings("~/work", tailnet="example.ts.net")
    sb = s["sandbox"]
    assert sb["enabled"] and sb["failIfUnavailable"] and sb["autoAllowBashIfSandboxed"]
    assert sb["network"]["allowedDomains"] == ["*.example.ts.net"]   # no internet, tailnet only
    assert sb["network"]["strictAllowlist"] is True                   # refuse, don't prompt
    assert sb["filesystem"]["allowWrite"] == [f"{HOME}/work", "/tmp"]
    secrets, state = f"{HOME}/.config/agentic-lab", f"{HOME}/.local/state/agentic-lab"
    nebius = f"{HOME}/.nebius"                  # the secretary's VM-power key (#40)
    assert sb["filesystem"]["denyRead"] == ["/run", "/var/run", secrets, state, nebius]
    assert s["permissions"]["deny"] == [f"Read(/{secrets}/**)", f"Edit(/{secrets}/**)",
                                        f"Read(/{state}/**)", f"Edit(/{state}/**)",
                                        f"Read(/{nebius}/**)", f"Edit(/{nebius}/**)"]
    assert sb["credentials"]["envVars"] == [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"}]
    assert sb["filesystem"]["allowRead"] == []
    assert sb["network"]["allowAllUnixSockets"] is False           # seccomp socket block on
    assert s["permissions"]["defaultMode"] == "default"
    assert s["env"]["XDG_CACHE_HOME"] == f"{HOME}/work/.cache"     # caches inside the sandbox
    assert s["permissions"]["allow"] == [f"Edit(/{HOME}/work/**)", "Edit(//tmp/**)"]


def test_untrusted_tailnet():
    s = sandbox.settings("~/work", tailnet="example.ts.net", trust_tailnet=False)
    assert s["sandbox"]["network"]["allowedDomains"] == []


def test_standing_grants():
    gl = [G.from_mapping(m) for m in
          ({"domain": "pypi.org"}, {"write": "~/datasets"}, {"tool": "WebFetch"})]
    s = sandbox.settings("~/work", tailnet="t.ts.net", grant_list=gl)
    assert s["sandbox"]["network"]["allowedDomains"] == ["*.t.ts.net", "pypi.org"]
    assert f"{HOME}/datasets" in s["sandbox"]["filesystem"]["allowWrite"]
    assert "WebFetch" in s["permissions"]["allow"]
    assert f"Edit(/{HOME}/datasets/**)" in s["permissions"]["allow"]


def test_timed_grant_widens_sandbox_then_expires():
    g = G.timed("domain", "arxiv.org", dt.timedelta(hours=1), "alice", NOW)
    during = sandbox.settings("~/work", grant_list=[g], now=NOW + dt.timedelta(minutes=59))
    after = sandbox.settings("~/work", grant_list=[g], now=NOW + dt.timedelta(hours=1))
    assert "arxiv.org" in during["sandbox"]["network"]["allowedDomains"]
    assert "arxiv.org" not in after["sandbox"]["network"]["allowedDomains"]


def test_timed_grants_dropped_without_clock():
    # with no `now`, only standing grants count (never assume a timed grant is live)
    g = G.timed("domain", "arxiv.org", dt.timedelta(hours=1), "alice", NOW)
    assert sandbox.settings("~/work", grant_list=[g])["sandbox"]["network"]["allowedDomains"] == []


def test_timed_grant_needs_a_human():
    with pytest.raises(G.GrantError, match="name the human"):
        G.timed("domain", "arxiv.org", dt.timedelta(hours=1), None, NOW)
    with pytest.raises(G.GrantError, match="positive"):
        G.timed("domain", "arxiv.org", dt.timedelta(0), "alice", NOW)


def test_grant_str():
    g = G.timed("domain", "arxiv.org", dt.timedelta(hours=1), "alice", NOW)
    assert str(g) == "domain arxiv.org until 2026-10-17 13:00"


@pytest.mark.parametrize("m", [
    {"domain": "*.huggingface.co"}, {"domain": "localhost.example:8080"},
    {"write": "/data/shared"}, {"write": "~/datasets"},
    {"tool": "WebSearch"}, {"tool": "mcp__tracker__get_*"},
    {"domain": "arxiv.org", "reason": "papers"},
])
def test_valid_grants(m):
    G.from_mapping(m)


@pytest.mark.parametrize("m,msg", [
    ({"domain": "*"}, "not a domain"),
    ({"domain": "https://arxiv.org"}, "not a domain"),
    ({"write": "relative/dir"}, "absolute or ~/"),
    ({"write": "/"}, "too broad"),
    ({"write": "~/"}, "too broad"),
    ({"tool": "mcp__*"}, "anchored"),
    ({"tool": "*"}, "anchored"),
    ({"domain": "a.org", "tool": "WebFetch"}, "exactly one"),
    ({"host": "a.org"}, "exactly one"),
    ({"bash": "curl"}, "exactly one"),
    ("arxiv.org", "a mapping"),
])
def test_invalid_grants(m, msg):
    with pytest.raises(G.GrantError, match=msg):
        G.from_mapping(m)


def test_gpu_agent():
    sb = sandbox.settings("~/work", gpu=True)["sandbox"]
    # CUDA needs Unix sockets; /run stays hidden except nvidia-persistenced's socket dir
    assert sb["network"]["allowAllUnixSockets"] is True
    assert sb["filesystem"]["denyRead"][:2] == ["/run", "/var/run"]
    assert sb["filesystem"]["allowRead"] == ["/run/nvidia-persistenced"]
