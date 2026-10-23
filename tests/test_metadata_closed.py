"""The cloud's metadata service is closed to the lab's users, and lab doctor checks it."""
from pathlib import Path

import pytest

from agentic_lab import doctor as D
from agentic_lab.policy.sandbox import METADATA
from agentic_lab.worker import runs as R

ROOT = Path(__file__).parent.parent
UNITS = ["deploy/worker/lab-worker@.service", "deploy/manager/lab-manager@.service",
         "deploy/secretary/lab-secretary.service", "deploy/secretary/lab-broker.service",
         "deploy/secretary/lab-intake.service"]


@pytest.mark.parametrize("unit", UNITS)
def test_every_lab_unit_denies_it(unit):
    lines = (ROOT / unit).read_text().splitlines()
    assert f"IPAddressDeny={METADATA}" in lines


def test_so_does_every_run():
    assert f"IPAddressDeny={METADATA}" in R.unit_template("w", "lab-w").splitlines()


def test_both_addresses():
    assert METADATA.split() == ["169.254.169.254/32", "fd00:ec2::254/128"]


def doctor(monkeypatch, tmp_path, shown, root_reaches, agent_code):
    calls = []

    def sh(*argv, timeout=20):
        calls.append(argv)
        if argv[:2] == ("systemctl", "show"):
            return 0, shown
        if argv[0] == "systemd-run":
            return agent_code, ""
        return (0 if root_reaches else 1), ""
    monkeypatch.setattr(D, "sh", sh)
    t = tmp_path / "lab-run-w@.service"
    t.write_text(R.unit_template("w", "lab-w"))
    return D.metadata_checks(["lab-worker@w"], [str(t)], "lab-w"), calls


def test_doctor_without_a_metadata_service(monkeypatch, tmp_path):
    res, calls = doctor(monkeypatch, tmp_path, f"IPAddressDeny={METADATA}", False, None)
    assert [r.status for r in res] == [D.OK, D.OK] and "no cloud metadata" in res[1].what
    assert not any(c[0] == "systemd-run" for c in calls)


def test_doctor_when_it_is_closed(monkeypatch, tmp_path):
    res, calls = doctor(monkeypatch, tmp_path, f"IPAddressDeny={METADATA}", True, 1)
    assert [r.status for r in res] == [D.OK, D.OK] and "can't reach" in res[1].what
    probe = next(c for c in calls if c[0] == "systemd-run")
    assert "--uid=lab-w" in probe and f"--property=IPAddressDeny={METADATA}" in probe


def test_doctor_when_the_kernel_doesnt_enforce_it(monkeypatch, tmp_path):
    res, _ = doctor(monkeypatch, tmp_path, f"IPAddressDeny={METADATA}", True, 0)
    assert res[1].status == D.FAIL and "can reach" in res[1].what


def test_doctor_when_a_unit_lacks_it(monkeypatch, tmp_path):
    res, _ = doctor(monkeypatch, tmp_path, "IPAddressDeny=", False, None)
    assert res[0].status == D.FAIL and "lab-worker@w" in res[0].what


@pytest.mark.parametrize("conf,status", [
    ("nameserver 127.0.0.53\noptions edns0\n", D.OK),
    ("# GCP without resolved\nnameserver 169.254.169.254\nsearch c.example.internal\n", D.FAIL),
])
def test_doctor_fails_when_dns_goes_through_the_metadata_address(tmp_path, conf, status):
    real = tmp_path / "stub-resolv.conf"
    real.write_text(conf)
    (tmp_path / "resolv.conf").symlink_to(real)                    # followed, like Ubuntu's
    res = D.resolver_checks(str(tmp_path / "resolv.conf"))
    assert [r.status for r in res] == [status]
