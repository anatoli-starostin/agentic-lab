"""lab init writes a lab.yaml that lab check accepts."""
import pytest

from agentic_lab import registry
from agentic_lab.cli import main


def test_init_with_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agentic_lab.init.tailscale_self", lambda: ("example.ts.net",
                                                                    "sec.example.ts.net"))
    path = tmp_path / "lab.yaml"
    rc = main(["init", str(path), "--name", "deepspike", "--admin", "anatoly",
               "--admin-name", "Anatoly", "--slack-user", "U0123", "--manager", "mgr-anatoly",
               "--worker", "worker-a@gpu-a", "--worker", "worker-b@gpu-b=gpu-b.other.ts.net"])
    assert rc == 0 and "0 errors" in capsys.readouterr().out
    lab = registry.load(path)
    assert lab.hosts["sec"]["tailnet_name"] == "sec.example.ts.net"
    assert lab.hosts["gpu-a"]["tailnet_name"] == "gpu-a.example.ts.net"
    assert lab.hosts["gpu-b"]["tailnet_name"] == "gpu-b.other.ts.net"
    assert {a["role"] for a in lab.agents.values()} == {"secretary", "manager", "worker"}
    assert path.read_text().startswith("# The lab's description")
    with pytest.raises(SystemExit, match="exists"):
        main(["init", str(path), "--name", "x", "--admin", "y"])


def test_bad_worker_spec(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_lab.init.tailscale_self", lambda: ("t.ts.net", "s.t.ts.net"))
    with pytest.raises(SystemExit, match="id@host"):
        main(["init", str(tmp_path / "l.yaml"), "--name", "x", "--admin", "a",
              "--admin-name", "A", "--worker", "Worker A"])
