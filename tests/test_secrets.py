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


def test_failed_attempts_lock_the_intake(tmp_path):
    it = I.Intake(tmp_path)
    good = it.request("alice", "claude-token")
    for _ in range(I.MAX_FAILURES):
        with pytest.raises(I.IntakeError):
            it.receive("ZZZZ-ZZZZ", "claude-token", TOKEN)
    with pytest.raises(I.IntakeError, match="too many"):
        it.receive(good, "claude-token", TOKEN)


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
