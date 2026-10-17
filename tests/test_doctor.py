"""lab doctor: the checks that don't need a real host."""
import os
import pwd

from agentic_lab import doctor as D


def test_secret_checks(tmp_path, monkeypatch):
    from types import SimpleNamespace
    me = pwd.getpwuid(os.getuid())
    fake = SimpleNamespace(pw_dir=str(tmp_path), pw_uid=me.pw_uid, pw_name=me.pw_name)
    cfg = tmp_path / ".config" / "agentic-lab"
    cfg.mkdir(parents=True)
    (cfg / "slack.env").write_text("x")
    os.chmod(cfg / "slack.env", 0o600)
    (cfg / "workers.yaml").write_text("x")
    os.chmod(cfg / "workers.yaml", 0o644)
    res = {r.what.split()[1]: r.status for r in D.secret_checks(fake, ["slack.env", "workers.yaml",
                                                                         "broker.yaml"])}
    assert res == {"slack.env": D.OK, "workers.yaml": D.FAIL, "broker.yaml": D.FAIL}


def test_address_checks(free_port):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", free_port))
    s.listen(1)
    try:
        assert D.address_checks("127.0.0.1", free_port)[0].status == D.OK
    finally:
        s.close()
    assert D.address_checks("no-such-host.invalid", 1)[0].status == D.FAIL


def test_report_counts_failures():
    text, failed = D.report([("x", [D.Result(D.OK, "a"), D.Result(D.FAIL, "b", "fix it")])])
    assert failed == 1 and "✗ b" in text and "→ fix it" in text
