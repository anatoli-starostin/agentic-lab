"""`lab doctor`: is this host the way lab.yaml says it should be? (DESIGN.md §11a)

Run on a host with sudo. Checks the host (sandbox prerequisites, GPU through the sandbox's
wrapper), then each agent lab.yaml places here (its Unix user, its service, its secrets, its
address), then the lab-wide services running here (broker, secret intake). Every failure
comes with what to do about it. Uses no Claude tokens.
"""
import json
import os
import pwd
import shutil
import socket
import stat
import subprocess
from dataclasses import dataclass

from .registry import check as lab_check
from .registry import load as load_lab

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✓", WARN: "!", FAIL: "✗"}
BWRAP_GPU = "/usr/local/libexec/agentic-lab/bwrap-gpu"
MANAGED = "/etc/claude-code/managed-settings.json"
CONFIG = ".config/agentic-lab"

# the secrets each kind of service needs, in its user's ~/.config/agentic-lab
SECRETS = {
    "worker": ["claude-oauth-token", "manager-tokens.yaml", "broker.yaml"],
    "manager": ["claude-oauth-token", "slack.env", "workers.yaml", "broker.yaml"],
    "secretary": ["claude-oauth-token", "monitor.yaml", "broker.yaml"],
    "broker": ["slack.env", "agent-tokens.yaml"],
}
UNITS = {"worker": "lab-worker@{}", "manager": "lab-manager@{}", "secretary": "lab-secretary",
         "broker": "lab-broker", "intake": "lab-intake"}


@dataclass
class Result:
    status: str
    what: str
    hint: str = ""

    def __str__(self):
        return f"  {MARK[self.status]} {self.what}" + (f"\n      → {self.hint}" if self.hint else "")


def sh(*argv, timeout=20):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def this_host(lab):
    """Which lab.yaml host this machine is: by tailnet name, else by hostname."""
    code, out = sh("tailscale", "status", "--json")
    names = {socket.gethostname().split(".")[0]}
    if code == 0:
        try:
            dns = json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".")
            names |= {dns, dns.split(".")[0]}
        except ValueError:
            pass
    for hid, h in lab.hosts.items():
        h = h or {}
        if hid in names or h.get("tailnet_name") in names or h.get("instance") in names:
            return hid
    return None


# ── checks ───────────────────────────────────────────────────────────────────

def host_checks(gpu):
    out = []
    for tool in ("bwrap", "socat"):
        out.append(Result(OK, f"{tool} installed") if shutil.which(tool) else
                   Result(FAIL, f"{tool} missing", "run deploy/worker/setup-sandbox.sh"))
    code, val = sh("sysctl", "-n", "kernel.apparmor_restrict_unprivileged_userns")
    if val.strip() == "1":
        code, prof = sh("aa-status")
        loaded = os.path.exists("/etc/apparmor.d/bwrap") and "bwrap" in prof
        out.append(Result(OK, "AppArmor: bwrap profile for the sandbox") if loaded else
                   Result(FAIL, "AppArmor blocks the sandbox's user namespaces",
                          "run deploy/worker/setup-sandbox.sh (installs /etc/apparmor.d/bwrap)"))
    if gpu:
        try:
            with open(MANAGED) as f:
                managed = json.load(f)
        except (OSError, ValueError):
            managed = {}
        path = managed.get("sandbox", {}).get("bwrapPath")
        out.append(Result(OK, "managed settings point the sandbox at bwrap-gpu")
                   if path == BWRAP_GPU and os.access(BWRAP_GPU, os.X_OK) else
                   Result(FAIL, "the sandbox won't see the GPU (no bwrap-gpu in managed settings)",
                          "run deploy/worker/setup-sandbox.sh"))
        code, msg = sh(BWRAP_GPU, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                       "--unshare-user", "--unshare-net", "--die-with-parent", "--",
                       "nvidia-smi", "-L")
        out.append(Result(OK, f"GPU visible inside the sandbox ({msg.splitlines()[0][:60]})")
                   if code == 0 else
                   Result(FAIL, "GPU not visible through bwrap-gpu", msg[-200:]))
    return out


def aws_checks(lab, pw, ec2=None):
    """The secretary's AWS key (VM power, #68): present, its own, and it works:
    DescribeInstances for each AWS host it powers."""
    from .secretary import power as P
    hosts = {h: x for h, x in lab.hosts.items()
             if (x or {}).get("provider") == "aws" and (x or {}).get("instance")}
    if not hosts:
        return []
    out = secret_checks(pw, ["aws-key.json"])
    if out[0].status != OK:
        return out
    key = os.path.join(pw.pw_dir, CONFIG, "aws-key.json")
    for h, x in hosts.items():
        try:
            (ec2 or P._ec2)(x.get("region"), key=key).describe_instances(
                InstanceIds=[x["instance"]])
            out.append(Result(OK, f"AWS key works for {h}"))
        except Exception as e:
            out.append(Result(FAIL, f"AWS key can't describe {h}: {str(e)[:160]}",
                              "check the key, its policy and the host's instance/region"))
    return out


def run_checks(aid, user):
    """A worker's long runs: its unit template and sudo rule (worker/runs.py)."""
    from .worker.runs import RUN_CTL
    out = []
    if os.path.exists(f"/etc/systemd/system/lab-run-{aid}@.service") and os.path.exists(RUN_CTL):
        out.append(Result(OK, "long runs: unit template and lab-run-ctl installed"))
    else:
        out.append(Result(FAIL, "long runs can't start (no unit template or lab-run-ctl)",
                          "sudo lab apply"))
    try:
        with open("/etc/sudoers.d/agentic-lab-runs") as f:
            ok = f"{user} ALL=(root) NOPASSWD: {RUN_CTL}" in f.read()
    except OSError:
        ok = None                     # not readable without root: can't tell
    if ok is False:
        out.append(Result(FAIL, f"no sudo rule for {user}'s long runs", "sudo lab apply"))
    elif ok:
        out.append(Result(OK, "long runs: sudo rule"))
    if not os.access("/usr/bin/bwrap", os.X_OK):
        out.append(Result(FAIL, "long runs can't start: no /usr/bin/bwrap (they run in a "
                          "process namespace of their own)", "sudo lab apply"))
    return out


def user_checks(user):
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return [Result(FAIL, f"no Unix user {user}",
                       f"sudo deploy/worker/create-agent-user.sh {user}")], None
    out = [Result(OK, f"user {user}") if pw.pw_shell.endswith("nologin") else
           Result(WARN, f"{user} has a login shell ({pw.pw_shell})",
                  "agent users must not log in: usermod -s /usr/sbin/nologin " + user)]
    code, linger = sh("loginctl", "show-user", user, "-p", "Linger")
    if "Linger=yes" in linger:
        out.append(Result(WARN, f"{user} has lingering on", f"loginctl disable-linger {user}"))
    return out, pw


def secret_checks(pw, names):
    out = []
    for name in names:
        p = os.path.join(pw.pw_dir, CONFIG, name)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            out.append(Result(FAIL, f"secret {name} missing",
                              "the guided setup (`lab apply`) installs it; see docs/install.md"))
            continue
        if st.st_uid != pw.pw_uid or stat.S_IMODE(st.st_mode) & 0o077:
            out.append(Result(FAIL, f"secret {name} is readable by others or not owned by "
                                    f"{pw.pw_name}", f"chown {pw.pw_name} {p}; chmod 600 {p}"))
        else:
            out.append(Result(OK, f"secret {name}"))
    return out


def unit_checks(unit):
    code, active = sh("systemctl", "is-active", unit)
    code2, enabled = sh("systemctl", "is-enabled", unit)
    if active.strip() == "active":
        res = Result(OK, f"service {unit} running")
    else:
        res = Result(FAIL, f"service {unit} is {active.strip() or 'missing'}",
                     f"sudo journalctl -u {unit} -n 50   (then: sudo systemctl restart {unit})")
    out = [res]
    if enabled.strip() != "enabled":
        out.append(Result(WARN, f"{unit} doesn't start at boot", f"sudo systemctl enable {unit}"))
    return out


def address_checks(name, port):
    try:
        addr = socket.gethostbyname(name)
    except OSError:
        return [Result(FAIL, f"{name} doesn't resolve",
                       "set the host's tailnet_name in lab.yaml to its MagicDNS name")]
    try:
        with socket.create_connection((addr, port), timeout=5):
            return [Result(OK, f"reachable at {name}:{port}")]
    except OSError as e:
        return [Result(FAIL, f"nothing answers at {name}:{port} ({e.strerror or e})",
                       "is the service running and bound to the tailnet IP?")]


def applied_checks(path="/etc/agentic-lab/applied.yaml"):
    """Which configuration and framework commits this host runs, and were they reviewed?"""
    import yaml
    try:
        with open(path) as f:
            a = yaml.safe_load(f) or {}
    except OSError:
        return [Result(WARN, "no record of what was applied here",
                       "sudo lab apply (from the secretary host)")]
    what = (f"config {str(a.get('config_commit', '?'))[:10]} ({a.get('branch') or '?'}), "
            f"framework {str(a.get('framework_commit', '?'))[:10]}, applied "
            f"{a.get('applied_at', '?')} by {a.get('applied_by', '?')}")
    if a.get("unreviewed"):
        return [Result(WARN, f"UNREVIEWED: {what}",
                       "merge those changes to main via a PR, then sudo lab apply")]
    return [Result(OK, what)]


def run(lab_path, host=None):
    lab = load_lab(lab_path)
    sections = []
    issues = lab_check(lab)
    errs = [i for i in issues if i.level == "error"]
    sections.append(("lab.yaml", [Result(OK, "lab check: no errors") if not errs else
                                  Result(FAIL, f"lab check: {len(errs)} error(s)",
                                         f"lab check {lab_path}")] + applied_checks()))
    host = host or this_host(lab)
    if host is None:
        sections.append(("host", [Result(FAIL, "this machine isn't a host in lab.yaml",
                                         "pass --host <id>, or set the host's tailnet_name")]))
        return sections
    here = [aid for aid, a in lab.agents.items() if (a or {}).get("host") == host]
    gpu = any(lab.agents[a].get("gpu", lab.agents[a].get("role") == "worker") for a in here)
    sections.append((f"host {host}", host_checks(gpu)))

    for aid in here:
        role = lab.agents[aid].get("role")
        res, pw = user_checks(lab.unix_user(aid))
        if pw is not None:
            res += secret_checks(pw, SECRETS.get(role, []))
            res += aws_checks(lab, pw) if role == "secretary" else []
        unit = UNITS.get(role, "").format(aid)
        if unit:
            res += unit_checks(unit)
            res += address_checks(*lab.address(aid)) if role == "worker" else []
            res += run_checks(aid, lab.unix_user(aid)) if role == "worker" else []
        else:
            res.append(Result(WARN, f"{role} agents get a service in a later milestone"))
        sections.append((f"{role} {aid} ({lab.display_name(aid)})", res))

    # the intake itself runs only while a code is open: its path unit is what's always on
    for svc, unit in (("broker", "lab-broker"), ("intake", "lab-intake.path")):
        code, _ = sh("systemctl", "cat", unit)
        if code == 0:                                 # this host runs it
            res, pw = user_checks("lab-broker" if svc == "broker" else "lab-secretary")
            if pw is not None and svc == "broker":
                res += secret_checks(pw, SECRETS["broker"])
            res += unit_checks(unit)
            if svc == "intake" and sh("systemctl", "is-failed", "--quiet", "lab-intake")[0] == 0:
                res.append(Result(FAIL, "the intake failed when it last started (it runs only "
                                  "while a one-time code is open)",
                                  "sudo journalctl -u lab-intake -n 50"))
            sections.append((svc, res))
    return sections


def report(sections):
    lines, failed = [], 0
    for title, results in sections:
        lines.append(title)
        lines += [str(r) for r in results]
        failed += sum(r.status == FAIL for r in results)
    lines.append(f"\n{failed} problem(s)" if failed else "\nall good")
    return "\n".join(lines), failed
