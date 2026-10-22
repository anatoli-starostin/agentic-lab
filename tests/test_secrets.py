"""Secret intake: one-time codes, the HTTP endpoint, and the member client end to end."""
import datetime as dt
import importlib.util
import os
import stat
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from agentic_lab.cli import main
from agentic_lab.secrets import intake as I
from agentic_lab.secrets.server import app

ROOT = Path(__file__).parent.parent
TOKEN = "sk-ant-oat01-" + "x" * 60


def test_code_is_single_use_and_bound(tmp_path):
    it = I.Intake(tmp_path)
    code = it.request("alice", "claude-token")
    assert len(code) == 9 and code[4] == "-"
    assert it.receive(code.lower(), "claude-token", TOKEN) == "alice"   # case-insensitive
    assert it.load("alice", "claude-token") == TOKEN
    with pytest.raises(I.IntakeError, match="already used"):
        it.receive(code, "claude-token", TOKEN)


def test_secret_files_are_private(tmp_path):
    it = I.Intake(tmp_path)
    it.receive(it.request("alice", "claude-token"), "claude-token", TOKEN)
    p = tmp_path / "secrets" / "humans" / "alice" / "claude-token"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # only hashes of codes are kept
    assert all(len(f.stem) == 64 for f in (tmp_path / "secret-requests").glob("*.json"))


def test_bad_value_keeps_code_usable(tmp_path):
    it = I.Intake(tmp_path)
    code = it.request("alice", "claude-token")
    with pytest.raises(I.IntakeError, match="doesn't look like"):
        it.receive(code, "claude-token", "hunter2")
    assert it.receive(code, "claude-token", TOKEN) == "alice"


def test_expired_and_unknown(tmp_path):
    it = I.Intake(tmp_path)
    old = it.request("alice", "claude-token", ttl=dt.timedelta(seconds=-1))
    with pytest.raises(I.IntakeError, match="expired"):
        it.receive(old, "claude-token", TOKEN)
    with pytest.raises(I.IntakeError, match="unknown"):
        it.receive("AAAA-BBBB", "claude-token", TOKEN)
    with pytest.raises(I.IntakeError, match="unknown secret"):
        it.request("alice", "ssh-key")


def test_failed_attempts_lock_out_only_that_caller(tmp_path):
    """#63: bad codes from one device lock that device out, not everyone else."""
    it = I.Intake(tmp_path)
    good = it.request("alice", "claude-token")
    for _ in range(I.MAX_FAILURES):
        with pytest.raises(I.IntakeError):
            it.receive("ZZZZ-ZZZZ", "claude-token", TOKEN, source="100.64.0.9")
    with pytest.raises(I.IntakeError, match="too many"):
        it.receive(good, "claude-token", TOKEN, source="100.64.0.9")
    assert it.receive(good, "claude-token", TOKEN, source="100.64.0.2") == "alice"


def test_message_has_the_command():
    msg = I.message("Alice", "K7QM-XP4D", "http://sec.example.ts.net:8771/")
    assert ("python3 lab-member.py claude-token --code K7QM-XP4D "
            "--to http://sec.example.ts.net:8771") in msg
    assert "curl -fsSL http://sec.example.ts.net:8771/client/lab-member.py" in msg


def test_cli_request(tmp_path, capsys):
    lab = str(ROOT / "examples" / "minimal" / "lab.yaml")
    assert main(["--lab", lab, "secret", "request", "alice", "claude-token",
                 "--url", "http://s:8771", "--state", str(tmp_path)]) == 0
    assert "--code " in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no human"):
        main(["--lab", lab, "secret", "request", "bob", "claude-token",
              "--url", "http://s:8771", "--state", str(tmp_path)])


def _load_client():
    spec = importlib.util.spec_from_file_location("lab_member", ROOT / "client" / "lab-member.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_member_client_end_to_end(tmp_path, monkeypatch, free_port):
    it = I.Intake(tmp_path)
    code = it.request("alice", "claude-token")
    server = uvicorn.Server(uvicorn.Config(app(it, ROOT / "client" / "lab-member.py"),
                                           host="127.0.0.1", port=free_port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    try:
        url = f"http://127.0.0.1:{free_port}"
        client = _load_client()
        monkeypatch.setattr(client.getpass, "getpass", lambda prompt: TOKEN)
        client.main(["claude-token", "--paste", "--code", code, "--to", url])
        assert it.load("alice", "claude-token") == TOKEN
        with pytest.raises(SystemExit, match="refused it \\(403\\)"):        # code is used up
            client.main(["claude-token", "--paste", "--code", code, "--to", url])
        served = urllib.request.urlopen(url + "/client/lab-member.py").read()
        assert served == (ROOT / "client" / "lab-member.py").read_bytes()
    finally:
        server.should_exit = True
        th.join()


def test_client_finds_token_in_terminal_output():
    client = _load_client()
    tok = "sk-ant-oat01-" + "A1b2_C3d4-" * 9
    wrapped = ("\x1b[32m✓ Long-lived token created\x1b[0m\r\n\r\n  " + tok[:50] + "\r\n  "
               + tok[50:] + "\r\n\r\nStore it securely.\r\n")
    assert client.find_token(wrapped) == tok
    assert client.find_token("no token here") is None
    assert client.find_token("x sk-ant-oat01-" + "z" * 60 + " trailing") == "sk-ant-oat01-" + "z" * 60


def test_client_finds_token_in_a_tui_rendering():
    """The way Claude Code draws it: spaces as cursor moves, `\r\r\n` line ends, `ESC 7`/`ESC 8`,
    private CSI queries and hyperlinks, a token wrapped at the terminal's width."""
    client = _load_client()
    tok = "sk-ant-oat01-" + "Zq9_x-Lm3P" * 9 + "AbCdE"
    row = 79
    out = ("\x1b7\x1b8\x1b[2G\x1b[1mYour\x1b[7GOAuth\x1b[13Gtoken:\x1b[22m\r\r\n\r\r\n"
           "\x1b]8;id=1;https://example.com\x1b\\link\x1b]8;;\x1b\\\r\r\n"
           "\x1b[2G" + tok[:row] + "\x1b[>0q\r\r\n\x1b[2G" + tok[row:] + "  \r\r\n\r\r\n"
           "\x1b[2GStore\x1b[8Gthis\r\r\n")
    assert client.find_token(out) == tok
    padded = "  " + tok[:row] + "   \n  " + tok[row:] + "\n\n  Store it\n"     # padded rows
    assert client.find_token(padded) == tok
    assert client.find_token("  " + tok + " export X=1") == tok                 # a space ends it


def test_client_stops_at_the_end_of_the_token():
    """A token on one row, then text drawn with cursor moves for spaces: the text isn't
    more of the token (it was once read as "...AAAStorethistokensecurely")."""
    client = _load_client()
    tok = "sk-ant-oat01-" + "U4Gc-Qaop_" * 9 + "AAAAAA"
    out = ("\x1b[2G\x1b[1mYour\x1b[7GOAuth\x1b[13Gtoken:\r\r\n\r\r\n\x1b[2G" + tok + "\r\r\n"
           "\x1b[2G\r\r\n\x1b[2GStore\x1b[8Gthis\x1b[13Gtoken\x1b[19Gsecurely.\r\r\n")
    assert client.find_token(out) == tok
    tight = "\x1b[2G" + tok + "\r\r\n\x1b[2GStore\x1b[8Gthis\r\r\n"          # no blank row
    assert client.find_token(tight) == tok
    assert client.screen("\x1b[2GStore\x1b[8Gthis\x1b[13Gtoken") == [" Store this token"]


def test_intake_verifies_claude_tokens(tmp_path):
    def verify(value, owner=None):
        if value != TOKEN:
            raise I.IntakeError("Claude rejected this token (61 characters)")
    it = I.Intake(tmp_path, verify={"claude-token": verify})
    code = it.request("alice", "claude-token")
    with pytest.raises(I.IntakeError, match="rejected this token"):
        it.receive(code, "claude-token", TOKEN[:-1])
    assert it.load("alice", "claude-token") is None
    assert it.receive(code, "claude-token", TOKEN) == "alice"       # the code was still usable


def test_verify_claude_token_lets_unknowns_through(monkeypatch):
    import urllib.error

    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    def offline(req, timeout):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(I.IntakeError, match="rejected this token"):
        I.verify_claude_token(TOKEN)
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    I.verify_claude_token(TOKEN)                                    # can't tell: accepted


def test_client_pastes_when_the_found_token_is_rejected(tmp_path, monkeypatch, free_port):
    def verify(value, owner=None):
        if value != TOKEN:
            raise I.IntakeError("Claude rejected this token (40 characters)")
    it = I.Intake(tmp_path, verify={"claude-token": verify})
    code = it.request("alice", "claude-token")
    server = uvicorn.Server(uvicorn.Config(app(it, ROOT / "client" / "lab-member.py"),
                                           host="127.0.0.1", port=free_port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    try:
        client = _load_client()
        monkeypatch.setattr(client, "get_claude_token", lambda paste: TOKEN[:40])  # cut short
        monkeypatch.setattr(client.getpass, "getpass", lambda prompt: TOKEN)
        client.main(["claude-token", "--code", code, "--to", f"http://127.0.0.1:{free_port}"])
        assert it.load("alice", "claude-token") == TOKEN
    finally:
        server.should_exit = True
        th.join()


def test_slack_app_secret_for_a_service(tmp_path):
    it = I.Intake(tmp_path)
    code = it.request("service:broker", "slack-app")
    with pytest.raises(I.IntakeError, match="App-Level Token"):
        it.receive(code, "slack-app", {"bot": "xoxb-" + "1" * 30, "app": "nope"})
    it.receive(code, "slack-app", {"bot": "xoxb-" + "1" * 30, "app": "xapp-" + "2" * 30})
    assert it.load("service:broker", "slack-app") == (
        "SLACK_BOT_TOKEN=xoxb-" + "1" * 30 + "\nSLACK_APP_TOKEN=xapp-" + "2" * 30)
    assert (tmp_path / "secrets" / "services" / "broker" / "slack-app").exists()
    with pytest.raises(I.IntakeError, match="bad owner"):
        it.request("../etc", "slack-app")


def test_web_form(tmp_path, free_port):
    it = I.Intake(tmp_path)
    code = it.request("service:broker", "slack-app")
    server = uvicorn.Server(uvicorn.Config(app(it, ROOT / "client" / "lab-member.py"),
                                           host="127.0.0.1", port=free_port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    try:
        import httpx2
        url = I.form_url(code, f"http://127.0.0.1:{free_port}")
        page = httpx2.get(url)
        assert page.status_code == 200 and 'type="password"' in page.text
        assert "Content-Security-Policy" in page.headers
        bad = httpx2.post(url, data={"bot": "xoxb-" + "1" * 30, "app": "oops"})
        assert "doesn&#x27;t look like" in bad.text or "doesn't look like" in bad.text
        assert "oops" not in bad.text                      # values are never echoed back
        ok = httpx2.post(url, data={"bot": "xoxb-" + "1" * 30, "app": "xapp-" + "2" * 30})
        assert "Stored" in ok.text
        assert httpx2.get(url).status_code == 404          # the link works once
        assert it.load("service:broker", "slack-app").startswith("SLACK_BOT_TOKEN=xoxb-")
        # one-field secrets, whatever their field's name (#37: github-app's is private_key)
        pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n-----END RSA PRIVATE KEY-----"
        for owner, name, field, value in (
                ("service:github-bot", "github-app", "private_key", pem),
                ("alice", "github-token", "value", "github_pat_" + "a" * 40)):
            url = I.form_url(it.request(owner, name), f"http://127.0.0.1:{free_port}")
            r = httpx2.post(url, data={field: value})
            assert r.status_code == 200 and "Stored" in r.text, r.text
            assert it.load(owner, name).strip() == value
    finally:
        server.should_exit = True
        th.join()
