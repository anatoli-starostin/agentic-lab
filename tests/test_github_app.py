"""GitHub Apps as agents' identities: schema, the app's JWT, minting and refreshing tokens,
what the agent's git and gh see, and both kinds of identity side by side."""
import asyncio
import datetime as dt
import os
import subprocess
from pathlib import Path

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agentic_lab import github as GH
from agentic_lab import github_app as GA
from agentic_lab import registry
from agentic_lab.apply import host_plan
from agentic_lab.secrets import intake as I

SINGLE = Path(__file__).parent.parent / "examples" / "single-vm" / "lab.yaml"


@pytest.fixture(scope="module")
def key():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return pem, k.public_key()


def lab_with_both(tmp_path):
    """A manager with a GitHub App, a worker with a full bot account: both kinds at once."""
    d = yaml.safe_load(SINGLE.read_text())
    d["github"] = {"accounts": {
        "acme-flash": {"app_id": 123456, "email": "999+acme-flash[bot]@users.noreply.github.com"},
        "worker-bot": {}}}
    for a in d["agents"].values():
        if a["role"] == "manager":
            a["github"] = "acme-flash"
        elif a["role"] == "worker":
            a["github"] = "worker-bot"
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    return registry.load(p)


def first(lab, role):
    return next(a for a, x in lab.agents.items() if x["role"] == role)


def test_both_kinds_side_by_side(tmp_path):
    lab = lab_with_both(tmp_path)
    assert [i for i in registry.check(lab) if i.level == "error"] == []
    mgr, worker = first(lab, "manager"), first(lab, "worker")
    assert lab.github_is_app("acme-flash") and not lab.github_is_app("worker-bot")
    assert lab.github_login("acme-flash") == "acme-flash[bot]"
    assert lab.github_email("acme-flash").startswith("999+acme-flash[bot]")
    assert "GitHub App acme-flash[bot]" in GH.instructions(lab, mgr)
    assert "bot account worker-bot" in GH.instructions(lab, worker)
    # deploy: the app's key for the manager, the token for the worker
    secrets = {("service:github-acme-flash", "github-app"): "-----BEGIN PRIVATE KEY-----x",
               ("service:github-worker-bot", "github-token"): "ghp_" + "w" * 36}

    class Tok:
        def get(self, k):
            return "t"
    plan = host_plan(lab, lab.agents[mgr]["host"], Tok(), lambda o, n: secrets.get(
        (o, n), None if n in ("nebius-sa", "github-app", "github-token") else "secret"))
    files = {p: c for p, (_, _, c) in plan["files"].items()}
    cfg = "/home/{}/.config/agentic-lab/github"
    assert files[f"{cfg.format(lab.unix_user(mgr))}/acme-flash.pem"].startswith("-----BEGIN")
    assert files[f"{cfg.format(lab.unix_user(worker))}/worker-bot.token"].startswith("ghp_")


def test_app_jwt_and_minting(key):
    pem, pub = key
    claims = jwt.decode(GA.app_jwt(123456, pem, now=1_000_000), pub, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "123456" and claims["exp"] - claims["iat"] == 600
    calls = []

    def call(method, path, bearer, body=None):
        calls.append((method, path))
        if path == "/app/installations":
            return [{"id": 42}]
        return {"token": "ghs_abc", "expires_at": "2026-10-19T12:00:00Z"}
    token, expires = GA.mint(123456, pem, call=call)
    assert token == "ghs_abc" and expires.tzinfo and calls[-1] == (
        "POST", "/app/installations/42/access_tokens")
    with pytest.raises(GA.AppError, match="isn't installed"):
        GA.mint(1, pem, call=lambda *a, **k: [])
    with pytest.raises(GA.AppError, match="several places"):
        GA.mint(1, pem, call=lambda *a, **k: [{"id": 1}, {"id": 2}])
    info = GA.check(123456, pem, users=lambda login: {"id": 999}, call=lambda m, p, b, body=None: (
        {"slug": "acme-flash"} if p == "/app" else
        [{"id": 42, "account": {"login": "acme"}, "repository_selection": "selected"}]))
    assert info["bot"] == "acme-flash[bot]"
    assert info["email"] == "999+acme-flash[bot]@users.noreply.github.com"


def test_token_kept_where_git_and_gh_read_it(tmp_path):
    keyfile = tmp_path / "app.pem"
    keyfile.write_text("k")
    tdir = tmp_path / "gh-token"
    stop = asyncio.Event()

    def fake_mint(app_id, key, inst):
        stop.set()                                   # one round is enough here
        return "ghs_fresh", dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    listed = []

    def lister(token, app):                          # the installation's repositories (#53)
        listed.append((token, app))
        return ["acme/models", "acme/lab-config"]
    asyncio.run(GA.keep_token(1, "acme-flash[bot]", stop, key_path=str(keyfile),
                              token_dir=str(tdir), mint_fn=fake_mint, lister=lister))
    assert (tdir / "token").read_text() == "ghs_fresh\n"
    assert listed == [("ghs_fresh", True)]                       # with each new token
    assert GH.read_repos(str(tdir))[0] == ["acme/models", "acme/lab-config"]
    assert not (tdir / "gh" / "hosts.yml").exists()             # gh reads the token via its shim
    # the agent's git, with the app's token kept in its identity's folder
    lab = lab_with_both(tmp_path)
    root = tmp_path / "share"
    (root / "acme-flash").mkdir(parents=True)
    GA.write_token("ghs_fresh", "acme-flash[bot]", str(root / "acme-flash"))
    helper = Path(__file__).parent.parent / "deploy" / "worker" / "lab-git-credential"
    lab.agents[first(lab, "manager")]["workdir"] = str(tmp_path / "work")
    env = GH.prepare(lab, first(lab, "manager"), root=str(root), secrets=str(tmp_path / "s"),
                     helper=str(helper))
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "work" / ".config" / "gh")
    assert "GH_TOKEN" not in env                                  # read at call time (shim)
    r = subprocess.run(["git", "credential", "fill"],
                       input="protocol=https\nhost=github.com\npath=acme/x.git\n\n",
                       capture_output=True, text=True, timeout=20, cwd=tmp_path,
                       env={**os.environ, **env, "HOME": str(tmp_path)})
    assert "username=x-access-token" in r.stdout and "password=ghs_fresh" in r.stdout


def test_intake_takes_app_keys(key):
    assert I.check_value("github-app", key[0]).startswith("-----BEGIN")
    with pytest.raises(I.IntakeError):
        I.check_value("github-app", "-----BEGIN nothing useful here, really-----")
