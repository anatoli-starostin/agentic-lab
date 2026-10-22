"""#64: a GitHub identity that stops working says so: in the agent's prompt, from gh, in
health checks, on the web UI and in a notice to admins."""
import asyncio
import datetime as dt
import io
import subprocess
import urllib.error
from pathlib import Path

import pytest

from agentic_lab import github as GH
from agentic_lab import github_app as GA
from agentic_lab.secretary import web

from test_github import first, lab_with_github

SHIM = Path(__file__).parent.parent / "deploy" / "worker" / "gh-shim" / "gh"


def test_several_installations_are_named():
    def call(method, path, jwt, body=None):
        return [{"id": 11, "account": {"login": "acme"}},
                {"id": 22, "account": {"login": "other-org"}}]
    with pytest.raises(GA.AppError) as e:
        GA.mint(1, _key(), call=call)
    assert "acme (installation_id: 11)" in str(e.value) and "other-org (installation_id: 22)" \
        in str(e.value)


def _key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


def test_an_app_that_cant_mint_reports_and_recovers(tmp_path, monkeypatch):
    keyfile = tmp_path / "app.pem"
    keyfile.write_text("k")
    tdir = tmp_path / "acme-flash"
    tdir.mkdir()
    (tdir / "token").write_text("ghs_old\n")              # expired: no fresh one was minted
    stop = asyncio.Event()
    seen = []

    def mint(app_id, key, inst):
        seen.append((GH.read_status(str(tdir)), (tdir / "token").exists()))
        if len(seen) <= 2:
            raise GA.AppError("GitHub POST /app/installations/1/access_tokens: 401 bad JWT")
        stop.set()
        return "ghs_new", dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    real_wait = asyncio.wait_for

    async def no_wait(aw, timeout):                   # no real pause between rounds
        return await real_wait(aw, 0.01)
    monkeypatch.setattr(GA.asyncio, "wait_for", no_wait)
    asyncio.run(GA.keep_token(1, "acme-flash[bot]", stop, key_path=str(keyfile),
                              token_dir=str(tdir), mint_fn=mint, lister=lambda t, a: [],
                              skew_fn=lambda: 120.0))
    assert seen[0] == (None, True) and seen[1] == (None, True)     # one failure: not yet
    problem, token_left = seen[2]                                   # two: reported, and the
    assert "401 bad JWT" in problem and "120 s ahead of GitHub's" in problem
    assert token_left is False                                      # stale token is gone
    assert GH.read_status(str(tdir)) is None                        # recovered: cleared
    assert (tdir / "token").read_text() == "ghs_new\n"


def test_a_rejected_bot_token_is_reported(tmp_path):
    d = tmp_path / "flash-bot"
    d.mkdir()

    def rejected(token, app):
        raise urllib.error.HTTPError("https://api.github.com/user/repos", 401, "Bad credentials",
                                     {}, io.BytesIO(b""))
    assert not GH.refresh_repos("ghp_x", False, str(d), lister=rejected)
    assert "rejected (expired or revoked)" in GH.read_status(str(d))
    assert "service:github-flash-bot github-token" in GH.read_status(str(d))
    assert GH.refresh_repos("ghp_y", False, str(d), lister=lambda t, a: ["acme/x"])
    assert GH.read_status(str(d)) is None


def test_the_agent_and_gh_hear_about_it(tmp_path):
    lab = lab_with_github(tmp_path)
    mgr = first(lab, "manager")
    root = tmp_path / "share"
    (root / "flash-bot").mkdir(parents=True)
    GH.write_status(str(root / "flash-bot"), "its token was rejected")
    assert GH.problems(lab, mgr, root=str(root)) == {"flash-bot": "its token was rejected"}
    p = GH.instructions(lab, mgr, root=str(root))
    assert "doesn't work right now: its token was rejected" in p
    (root / "default").write_text("flash-bot\n")
    real = tmp_path / "real-gh"
    real.write_text("#!/bin/sh\nexit 4\n")
    real.chmod(0o755)
    r = subprocess.run([str(SHIM), "pr", "list"], capture_output=True, text=True, cwd=tmp_path,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                            "LAB_GITHUB_DIR": str(root), "LAB_REAL_GH": str(real)})
    assert "identity flash-bot doesn't work right now: its token was rejected" in r.stderr


def test_the_web_ui_shows_it():
    state = {"lab": "t", "at": dt.datetime.now(dt.timezone.utc).isoformat(), "accounts": {},
             "approvals": {}, "agents": {"mgr-a": {"name": "Flash", "role": "manager",
                                                   "github": {"acme-flash": "no token"}}}}
    assert "⚠ GitHub acme-flash: no token" in web.render(state)
