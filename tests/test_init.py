"""lab init writes a lab.yaml that lab check accepts."""
import pytest

from agentic_lab import registry
from agentic_lab.cli import main


def test_init_with_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agentic_lab.init.tailscale_self", lambda: ("example.ts.net",
                                                                    "sec.example.ts.net"))
    monkeypatch.setattr("agentic_lab.init.tailscale_login", lambda: "admin@example.com")
    path = tmp_path / "lab.yaml"
    rc = main(["init", str(path), "--name", "acme", "--admin", "anatoly",
               "--admin-name", "Anatoly", "--slack-user", "U0123", "--manager", "mgr-anatoly",
               "--worker", "worker-a@gpu-a", "--worker", "worker-b@gpu-b=gpu-b.other.ts.net",
               "--display", "mgr-anatoly=Flash", "--display", "worker-a=Nebius-the-Great"])
    assert rc == 0 and "0 errors" in capsys.readouterr().out
    lab = registry.load(path)
    assert lab.hosts["sec"]["tailnet_name"] == "sec.example.ts.net"
    assert lab.hosts["gpu-a"]["tailnet_name"] == "gpu-a.example.ts.net"
    assert lab.hosts["gpu-b"]["tailnet_name"] == "gpu-b.other.ts.net"
    assert {a["role"] for a in lab.agents.values()} == {"secretary", "manager", "worker"}
    assert lab.display_name("mgr-anatoly") == "Flash"
    assert lab.display_name("worker-a") == "Nebius-the-Great"
    assert lab.display_name("secretary") == "secretary"
    assert lab.humans["anatoly"]["admin"] and lab.humans["anatoly"]["tailscale_login"] == "admin@example.com"
    assert path.read_text().startswith("# The lab's description")
    with pytest.raises(SystemExit, match="exists"):
        main(["init", str(path), "--name", "x", "--admin", "y"])


def test_bad_worker_spec(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_lab.init.tailscale_self", lambda: ("t.ts.net", "s.t.ts.net"))
    with pytest.raises(SystemExit, match="id@host"):
        main(["init", str(tmp_path / "l.yaml"), "--name", "x", "--admin", "a",
              "--admin-name", "A", "--worker", "Worker A"])


def test_manager_on_its_own_vm(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_lab.init.tailscale_self", lambda: ("t.ts.net", "sec.t.ts.net"))
    monkeypatch.setattr("agentic_lab.init.tailscale_login", lambda: None)
    path = tmp_path / "lab.yaml"
    main(["init", str(path), "--name", "x", "--admin", "a", "--admin-name", "A",
          "--manager", "mgr-a", "--manager-host", "mgr-vm"])
    lab = registry.load(path)
    assert lab.agents["mgr-a"]["host"] == "mgr-vm"
    assert lab.hosts["mgr-vm"]["tailnet_name"] == "mgr-vm.t.ts.net"
