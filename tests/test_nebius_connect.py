"""`lab nebius connect` against a fake Nebius CLI: the power role can't list VMs but can get
them; running it again changes nothing."""
from types import SimpleNamespace

import pytest
import yaml

from agentic_lab import nebius_connect as NC


class FakeNebius:
    """Just enough of the CLI. The secretary's profile `lab` can't list instances (like the
    real compute.instance-power-operator role), only get them."""

    def __init__(self, home):
        self.home, self.calls, self.keys = home, [], 0
        self.state = {"sa": None, "group": None, "permit": False, "member": False}

    def __call__(self, user, *args, profile=None, check=True):
        self.calls.append((user, args, profile))
        a = " ".join(args)
        if a == "profile list":
            return "lab-admin\n"
        if "service-account get-by-name" in a:
            return '{"metadata": {"id": "sa-1"}}' if self.state["sa"] else ""
        if "service-account create" in a:
            self.state["sa"] = True
            return '{"metadata": {"id": "sa-1"}}'
        if "group get-by-name" in a:
            return '{"metadata": {"id": "g-1"}}' if self.state["group"] else ""
        if "group create" in a:
            self.state["group"] = True
            return '{"metadata": {"id": "g-1"}}'
        if "access-permit list" in a:
            return NC.ROLE if self.state["permit"] else "[]"
        if "access-permit create" in a:
            self.state["permit"] = True
            return ""
        if "list-members" in a:
            return "sa-1" if self.state["member"] else "[]"
        if "group-membership create" in a:
            self.state["member"] = True
            return ""
        if "auth-public-key create" in a:
            self.keys += 1
            return f'{{"metadata": {{"id": "key-{self.keys}"}}}}'
        if args[:2] == ("profile", "create") or args[:2] == ("profile", "update"):
            if args[0:2] == ("profile", "create") and (self.home / ".nebius" / "config.yaml").exists():
                raise NC.ConnectError('profile "lab" is already exists')
            flags = dict(zip(args[3::2], args[4::2]))
            (self.home / ".nebius" / "config.yaml").write_text(yaml.safe_dump({"profiles": {"lab": {
                "service-account-id": flags["--service-account-id"],
                "public-key-id": flags["--public-key-id"],
                "private-key-file-path": flags["--private-key-file-path"]}}}))
            return ""
        if "instance list" in a:
            if profile == "lab":
                raise NC.ConnectError("PermissionDenied")          # the power role
            return '{"items": [{"metadata": {"id": "inst-a", "name": "gpu-a"}}]}'
        if "instance get" in a:
            return '{"metadata": {"id": "inst-a"}}' if "inst-a" in args else "not found"
        raise AssertionError(f"unexpected nebius call: {a}")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    (tmp_path / ".nebius").mkdir()
    (tmp_path / ".nebius" / "lab-key.pem").write_text("key")
    (tmp_path / ".nebius" / "lab-key.pub.pem").write_text("pub")
    f = FakeNebius(tmp_path)
    monkeypatch.setattr(NC, "neb", f)
    monkeypatch.setattr(NC, "_nebius_bin", lambda user: "/bin/nebius")
    monkeypatch.setattr(NC, "run_as", lambda *a, **k: "")
    monkeypatch.setattr(NC.pwd, "getpwnam", lambda u: SimpleNamespace(pw_dir=str(tmp_path)))
    return f


def test_connect_checks_with_get_and_runs_again_without_changes(fake):
    said = []
    NC.connect("admin", "lab-secretary", "proj", say=said.append,
               instances={"gpu-a": "inst-a"})
    assert any("can see gpu-a" in s for s in said)
    assert any("inst-a  gpu-a" in s for s in said)            # listed with the admin's login
    assert fake.keys == 1
    calls = len(fake.calls)
    said.clear()
    NC.connect("admin", "lab-secretary", "proj", say=said.append,
               instances={"gpu-a": "inst-a"})                 # again: no new key, no error
    assert fake.keys == 1 and any("already there" in s for s in said)
    new = [c[1] for c in fake.calls[calls:]]
    assert not any(a[:2] in (("profile", "create"), ("profile", "update")) for a in new)


def test_a_vm_the_secretary_cant_see_fails_the_check(fake):
    with pytest.raises(NC.ConnectError, match="can't see gpu-b"):
        NC.connect("admin", "lab-secretary", "proj", say=lambda s: None,
                   instances={"gpu-a": "inst-a", "gpu-b": "inst-b"})
