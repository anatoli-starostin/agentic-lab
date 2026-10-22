"""`lab plan` / `lab apply`: make the hosts match lab.yaml (DESIGN.md §11a).

Run on the secretary host, as root (sudo): it reads the secrets members and admins handed
over (the intake's store) and the lab's own generated tokens. For each host it builds a
bundle: the framework's source, lab.yaml, that host's secrets, and a generated, idempotent
shell script. The bundle is copied to the host (over SSH as the admin, or locally for this
host), run with sudo, and deleted. Running it again is always safe.

Secrets:
  - humans' Claude tokens and Slack apps' tokens come from the intake (`lab secret request`);
  - the tokens agents use to reach each other (manager -> worker, agent -> broker) are
    generated here, kept root-only in PRIVATE, and never shown to anyone.
`lab plan` lists the steps per host and any secret still missing, and changes nothing.
"""
import os
import pwd
import secrets as pysecrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile

import yaml

from .registry import load as load_lab

PRIVATE = "/etc/agentic-lab/private"
INTAKE = "/home/lab-secretary/.local/state/agentic-lab/secrets"
PORTS = {"worker": 8770, "intake": 8771, "broker": 8772, "secretary": 8774}
UNREACHABLE = 255           # run_bundle: the host didn't answer (e.g. a stopped VM)
SSH_FAILED = 254            # run_bundle: it answered, but ssh failed (host key, login...)
_NO_ANSWER = ("timed out", "no route to host", "connection refused", "network is unreachable",
              "could not resolve hostname", "name or service not known")
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]   # never hang on a host
SOURCE_COPY = "/opt/agentic-lab/src"


class ApplyError(RuntimeError):
    pass


def _is_source(d):
    return os.path.exists(os.path.join(d, "pyproject.toml")) and os.path.isdir(
        os.path.join(d, "deploy")) and os.path.isdir(os.path.join(d, "src", "agentic_lab"))


def find_source():
    """The framework's source tree to ship: the checkout you're in, else the repo this
    package was installed from, else the copy `lab apply` keeps in /opt/agentic-lab/src."""
    if _is_source(os.getcwd()):
        return os.getcwd()
    try:                               # the checkout lab apply used last (apply-main.yaml)
        with open("/etc/agentic-lab/apply-main.yaml") as f:
            src = (yaml.safe_load(f) or {}).get("framework_source")
        if src and _is_source(src) and os.path.exists(os.path.join(src, ".git")):
            return src
    except (OSError, ValueError):
        pass
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        d = os.path.dirname(d)
        if os.path.exists(os.path.join(d, "pyproject.toml")) and os.path.isdir(
                os.path.join(d, "deploy")):
            return d
    if os.path.exists(os.path.join(SOURCE_COPY, "pyproject.toml")):
        return SOURCE_COPY
    raise ApplyError("can't find the framework's source: pass --source <repo checkout>")


# ── the lab's own tokens ─────────────────────────────────────────────────────

class Tokens:
    """Generated tokens, kept root-only. Keys: `mgr:<manager>:<worker>`, `broker:<agent>`."""

    def __init__(self, root=PRIVATE):
        self.path = os.path.join(root, "tokens.yaml")
        try:
            with open(self.path) as f:
                self.data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.data = {}
        self.changed = False

    def get(self, key):
        if key not in self.data:
            self.data[key] = pysecrets.token_hex(32)
            self.changed = True
        return self.data[key]

    def save(self):
        if not self.changed:
            return
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(self.data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def intake_secret(owner, name, root=INTAKE):
    sub = ("services", owner[8:]) if owner.startswith("service:") else ("humans", owner)
    try:
        with open(os.path.join(root, *sub, name)) as f:
            return f.read().strip() + "\n"
    except FileNotFoundError:
        return None


# ── what each host needs ─────────────────────────────────────────────────────

def secretary_host(lab):
    for aid, a in lab.agents.items():
        if (a or {}).get("role") == "secretary":
            return a.get("host"), aid
    raise ApplyError("lab.yaml has no secretary")


def host_name(lab, host):
    h = lab.hosts.get(host) or {}
    return h.get("tailnet_name") or f"{host}.{lab.data.get('tailnet')}"


def host_plan(lab, host, tokens, intake=intake_secret):
    """Everything host `host` needs: files to install (path -> (owner, mode, content)),
    users, units, and the secrets that are missing (blocking)."""
    sec_host, secretary = secretary_host(lab)
    broker_url = f"http://{host_name(lab, sec_host)}:{PORTS['broker']}"
    name = host_name(lab, host)
    files, users, units, envs, missing, later = {}, [], [], {}, [], []
    run_units = {}              # worker -> its user: the unit template for its long runs
    run_workdirs = {}           # worker -> its working folder (writable by its runs)
    waiting = set()             # users whose service can't start yet (a secret is missing)

    def secret(user, fname, content, what):
        if content is None:
            missing.append(f"{what} (for {user})")
            if fname == "slack.env":    # a Slack-facing service can't run without its app;
                waiting.add(user)       # the rest start, and the intake must (it receives them)
            return
        files[f"/home/{user}/.config/agentic-lab/{fname}"] = (user, "600", content)

    here = {aid: a for aid, a in lab.agents.items() if (a or {}).get("host") == host}
    managers = {aid: a for aid, a in lab.agents.items() if (a or {}).get("role") == "manager"}
    for aid, a in here.items():
        user, role = lab.unix_user(aid), a.get("role")
        users.append(user)
        secret(user, "claude-oauth-token", intake(a["human"], "claude-token"),
               f"{a['human']}'s Claude token (lab secret request {a['human']} claude-token)")
        if role == "worker":
            allowed = [m for m, ma in managers.items()
                       if a.get("access", "dedicated") == "shared" or ma.get("human") == a.get("human")]
            files[f"/home/{user}/.config/agentic-lab/manager-tokens.yaml"] = (user, "600", yaml.safe_dump(
                {**{m: tokens.get(f"mgr:{m}:{aid}") for m in allowed},
                 secretary: tokens.get(f"mon:{aid}")}))
            files[f"/home/{user}/.config/agentic-lab/broker.yaml"] = (user, "600", yaml.safe_dump(
                {"url": broker_url, "token": tokens.get(f"broker:{aid}")}))
            _, port = lab.address(aid)
            envs[f"/etc/agentic-lab/worker-{aid}.env"] = f"LAB_BIND=@TAILNET_IP@\nLAB_PORT={port}\nLAB_NAME={name}\n"
            units.append(("lab-worker@.service", f"lab-worker@{aid}"))
            run_units[aid] = user
            run_workdirs[aid] = os.path.normpath((a.get("workdir") or "~/work").replace(
                "~", f"/home/{user}", 1))
        elif role == "manager":
            workers = [w for w, wa in lab.agents.items() if wa.get("role") == "worker" and (
                wa.get("access", "dedicated") == "shared" or wa.get("human") == a.get("human"))]
            files[f"/home/{user}/.config/agentic-lab/workers.yaml"] = (user, "600", yaml.safe_dump(
                {w: tokens.get(f"mgr:{aid}:{w}") for w in workers}))
            files[f"/home/{user}/.config/agentic-lab/broker.yaml"] = (user, "600", yaml.safe_dump(
                {"url": broker_url, "token": tokens.get(f"broker:{aid}")}))
            secret(user, "slack.env", intake(f"service:{aid}", "slack-app"),
                   f"{aid}'s Slack app tokens (lab secret request service:{aid} slack-app)")
            files[f"/home/{user}/.config/agentic-lab/monitor-token"] = (
                user, "600", tokens.get(f"mon:{aid}") + "\n")
            _, port = lab.address(aid)
            envs[f"/etc/agentic-lab/manager-{aid}.env"] = f"LAB_BIND=@TAILNET_IP@\nLAB_PORT={port}\n"
            units.append(("lab-manager@.service", f"lab-manager@{aid}"))
        for bot in lab.github_names(aid):    # its GitHub identities' secrets (service only)
            kind, ext = (("github-app", "pem") if lab.github_is_app(bot)
                         else ("github-token", "token"))
            tok = intake(f"service:github-{bot.lower()}", kind)
            if tok is None:             # optional: the agent runs, just without it yet
                later.append(f"{bot}'s GitHub {'App key' if kind == 'github-app' else 'token'}"
                             f" (lab secret request service:github-{bot.lower()} {kind})")
            else:
                files[f"/home/{user}/.config/agentic-lab/github/{bot}.{ext}"] = (
                    user, "600", tok.strip() + "\n")

    if host == sec_host:
        sec_user = lab.unix_user(secretary)
        workers_all = [w for w, wa in lab.agents.items() if wa.get("role") in ("worker", "manager")]
        files[f"/home/{sec_user}/.config/agentic-lab/monitor.yaml"] = (sec_user, "600", yaml.safe_dump(
            {w: tokens.get(f"mon:{w}") for w in workers_all}))
        files[f"/home/{sec_user}/.config/agentic-lab/broker.yaml"] = (sec_user, "600", yaml.safe_dump(
            {"url": broker_url, "token": tokens.get(f"broker:{secretary}")}))
        slack = intake(f"service:{secretary}", "slack-app")
        if slack is not None:          # optional: without it, the secretary has no Slack
            files[f"/home/{sec_user}/.config/agentic-lab/slack.env"] = (sec_user, "600", slack)
        sa = intake(f"service:{secretary}", "nebius-sa")
        if sa is not None:             # optional: without it, no VM power
            import json as _json
            sa = _json.loads(sa)
            key = f"/home/{sec_user}/.nebius/lab-key.pem"
            files[key] = (sec_user, "600", sa["private_key"].strip() + "\n")
            files[f"/home/{sec_user}/.nebius/config.yaml"] = (sec_user, "600", yaml.safe_dump({
                "default": "lab", "profiles": {"lab": {
                    "endpoint": "api.nebius.cloud", "auth-type": "service account",
                    "service-account-id": sa["service_account_id"],
                    "public-key-id": sa["public_key_id"], "private-key-file-path": key}}}))
            nebius_cli = sec_user
        if any((h or {}).get("provider") == "nebius" for h in lab.hosts.values()):
            nebius_cli = sec_user      # VM power: the CLI the secretary (and nebius connect) use
        users += ["lab-broker"]
        agents = [aid for aid, a in lab.agents.items()
                  if a.get("role") in ("worker", "manager", "secretary")]
        files["/home/lab-broker/.config/agentic-lab/agent-tokens.yaml"] = ("lab-broker", "600",
            yaml.safe_dump({aid: tokens.get(f"broker:{aid}") for aid in agents}))
        secret("lab-broker", "slack.env", intake("service:broker", "slack-app"),
               "the Lab Approvals Slack app tokens (lab secret request service:broker slack-app)")
        envs["/etc/agentic-lab/broker.env"] = f"LAB_BIND=@TAILNET_IP@\nLAB_PORT={PORTS['broker']}\n"
        envs["/etc/agentic-lab/intake.env"] = f"LAB_BIND=@TAILNET_IP@\nLAB_PORT={PORTS['intake']}\n"
        envs["/etc/agentic-lab/secretary.env"] = f"LAB_BIND=@TAILNET_IP@\nLAB_PORT={PORTS['secretary']}\n"
        units += [("lab-broker.service", "lab-broker"), ("lab-intake.service", "lab-intake"),
                  ("lab-secretary.service", "lab-secretary")]
        if "lab-secretary" not in users:
            users.append("lab-secretary")
    unit_user = {u: (f"lab-{u.split('@', 1)[1]}" if "@" in u else
                     {"lab-broker": "lab-broker", "lab-intake": "lab-secretary",
                      "lab-secretary": "lab-secretary"}.get(u)) for _, u in units}
    held = sorted(u for u, owner in unit_user.items() if owner in waiting)
    # who may read its agents' working folders (#43): {agent user: its working folder}
    readable = {lab.unix_user(aid): os.path.normpath(
                    (a.get("workdir") or "~/work").replace("~", f"/home/{lab.unix_user(aid)}", 1))
                for aid, a in here.items() if a.get("role") in ("worker", "manager")}
    return {"host": host, "name": name, "files": files, "users": users, "units": units,
            "run_units": run_units, "run_workdirs": run_workdirs,
            "readers": list((lab.hosts.get(host) or {}).get("readers") or []),
            "readable": readable,
            "envs": envs, "missing": missing, "later": later,
            "secretary_host": host == sec_host,
            "nebius_cli": locals().get("nebius_cli"), "held": held,
            "ssh_user": (lab.hosts.get(host) or {}).get("ssh_user")}


# ── the script each host runs ────────────────────────────────────────────────

SCRIPT_HEAD = r"""#!/bin/bash
# Generated by `lab apply` for host {host}. Idempotent: safe to run again.
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd)
say() {{ echo "== $*"; }}
TIP=$(tailscale ip -4 2>/dev/null | head -1 || true)
[ -n "$TIP" ] || {{ echo "this host isn't on the tailnet"; exit 1; }}

say "framework -> /opt/agentic-lab"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null 2>&1
mkdir -p /opt/agentic-lab /etc/agentic-lab
rm -rf /opt/agentic-lab/src.new && mkdir /opt/agentic-lab/src.new
tar xzf "$B/source.tgz" -C /opt/agentic-lab/src.new
rm -rf /opt/agentic-lab/src && mv /opt/agentic-lab/src.new /opt/agentic-lab/src
# a Python uv downloads (none 3.12 on the host) must be readable by the lab's users
export UV_PYTHON_INSTALL_DIR=/opt/agentic-lab/python
uv venv -q --allow-existing /opt/agentic-lab/venv -p 3.12
uv pip install -q -p /opt/agentic-lab/venv --reinstall-package agentic-lab /opt/agentic-lab/src
install -d /opt/agentic-lab/client
install -m 644 /opt/agentic-lab/src/client/lab-member.py /opt/agentic-lab/client/
install -m 644 "$B/lab.yaml" /etc/agentic-lab/lab.yaml
install -m 644 "$B/applied.yaml" /etc/agentic-lab/applied.yaml   # which commits run here
ln -sf /opt/agentic-lab/venv/bin/lab /usr/local/bin/lab     # `lab` for every user here

say "sandbox prerequisites"
/opt/agentic-lab/src/deploy/worker/setup-sandbox.sh

say "keep-awake check (anyone's ~/.lab-keep-awake keeps this machine on)"
install -m 644 /opt/agentic-lab/src/deploy/worker/lab-keep-awake.service \
    /opt/agentic-lab/src/deploy/worker/lab-keep-awake.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lab-keep-awake.timer >/dev/null 2>&1
/opt/agentic-lab/src/deploy/worker/lab-keep-awake.sh
"""


def script(plan):
    lines = [SCRIPT_HEAD.format(host=plan["host"])]
    lines.append('say "users"')
    for u in dict.fromkeys(plan["users"]):
        lines.append(f"/opt/agentic-lab/src/deploy/worker/create-agent-user.sh {u} >/dev/null")
    if plan.get("nebius_cli"):
        u = plan["nebius_cli"]
        lines.append('say "Nebius CLI (VM power)"')
        # the documented installer; SHELL=/bin/sh keeps it from editing shell rc files
        lines.append(f"sudo -u {u} -H bash -c 'test -x ~/.nebius/bin/nebius || "
                     "curl -sSL https://artifacts.nebius.cloud/cli/install.sh | "
                     "SHELL=/bin/sh bash >/dev/null'")
    lines.append('say "secrets and config"')
    for i, (path, (owner, mode, _)) in enumerate(sorted(plan["files"].items())):
        d = os.path.dirname(path)
        lines.append(f"install -d -m 700 -o {owner} -g {owner} {shlex.quote(os.path.dirname(d))} "
                     f"{shlex.quote(d)}")
        lines.append(f'install -m {mode} -o {owner} -g {owner} "$B/files/{i}" {shlex.quote(path)}')
    for i, path in enumerate(sorted(plan["envs"])):
        lines.append(f'sed "s/@TAILNET_IP@/$TIP/" "$B/envs/{i}" > {shlex.quote(path)}')
    if plan.get("self_admin"):
        lines += self_admin_script(plan["self_admin"])
    lines.append('say "services"')
    for unit_file, _ in plan["units"]:
        sub = "worker" if "worker" in unit_file else "manager" if "manager" in unit_file else "secretary"
        lines.append(f"install -m 644 /opt/agentic-lab/src/deploy/{sub}/{unit_file} /etc/systemd/system/")
    lines += removed_agents_script(plan)
    if plan.get("run_units"):
        lines += run_units_script(plan["run_units"], plan.get("run_workdirs") or {})
    else:                       # no worker here (any more): no runs, no sudo rule for them
        lines.append("rm -f /etc/sudoers.d/agentic-lab-runs")
    lines += readers_script(plan.get("readers") or [], plan.get("readable") or {})
    lines.append("systemctl daemon-reload")
    held = set(plan.get("held") or [])
    for _, unit in plan["units"]:
        if unit in held:        # its secrets aren't all there yet: don't start it
            lines.append(f"systemctl disable --now {unit} >/dev/null 2>&1 || true")
        else:
            lines.append(f"systemctl enable {unit} >/dev/null 2>&1; systemctl restart {unit}")
    lines.append('sleep 3; say "status"')
    for _, unit in plan["units"]:
        note = " (waiting for its secrets)" if unit in held else ""
        lines.append(f'echo "  {unit}: $(systemctl is-active {unit}){note}"')
    return "\n".join(lines) + "\n"


def removed_agents_script(plan):
    """Agents no longer on this host (removed from lab.yaml, or moved): their services
    stopped and disabled, their env files and run units removed (#61). Their Unix users
    and working folders stay (docs/uninstall.md)."""
    keep = " ".join(sorted(u for _, u in plan["units"]
                           if u.startswith(("lab-worker@", "lab-manager@"))))
    runs = " ".join(sorted(f"lab-run-{a}@.service" for a in plan.get("run_units") or {}))
    return [
        'say "agents no longer here"',
        f'keep=" {keep} "; keep_runs=" {runs} "',
        "for f in /etc/systemd/system/multi-user.target.wants/lab-worker@*.service "
        "/etc/systemd/system/multi-user.target.wants/lab-manager@*.service; do",
        '    [ -e "$f" ] || [ -L "$f" ] || continue',
        '    u=$(basename "$f" .service)',
        '    case "$keep" in *" $u "*) continue ;; esac',
        '    systemctl disable --now "$u" >/dev/null 2>&1 || true',
        '    role=${u%%@*}; role=${role#lab-}; id=${u#*@}',
        '    rm -f "/etc/agentic-lab/$role-$id.env"',
        '    systemctl stop "lab-run-$id@*" >/dev/null 2>&1 || true   # its long runs',
        '    echo "  stopped $u (no longer in lab.yaml for this host)"',
        "done",
        "for f in /etc/systemd/system/lab-run-*@.service; do",
        '    [ -e "$f" ] || continue',
        '    case "$keep_runs" in *" $(basename "$f") "*) continue ;; esac',
        '    rm -f "$f"; echo "  removed $(basename "$f")"',
        "done",
    ]


def readers_script(readers, readable):
    """Plain Linux read access to agents' working folders for the host's `readers` (#43):
    the group lab-readers (exactly those users), and on each agent: its home traversable by
    the group and nothing else in it (everything but the working folder made private), the
    working folder readable, including what the agent writes later (a default ACL, which
    the agent's UMask=0077 doesn't override). Secrets and state stay closed. No readers:
    the group is emptied."""
    q = shlex.quote
    if not readers:
        return ["getent group lab-readers >/dev/null && gpasswd -M '' lab-readers >/dev/null "
                "|| true"]
    lines = ['say "readers (plain read access to the agents\' working folders)"',
             "command -v setfacl >/dev/null || apt-get install -y acl >/dev/null",
             "getent group lab-readers >/dev/null || groupadd lab-readers",
             "members=''"]
    for r in readers:
        lines.append(f"if id {q(r)} >/dev/null 2>&1; then members=\"$members,{r}\"; "
                     f"else echo '  no user {r} here: skipped'; fi")
    lines.append('gpasswd -M "${members#,}" lab-readers >/dev/null')
    for user, work in sorted(readable.items()):
        home = f"/home/{user}"
        if os.path.dirname(work) != home:
            lines.append(f"echo '  {user}: its working folder {work} is not directly in its "
                         "home: not shared'")
            continue
        lines += [
            f"install -d -o {user} -g {user} -m 700 {q(work)} {q(work + '/tmp')}",
            # everything in the home but the working folder: the agent's alone
            f"find {q(home)} -mindepth 1 -maxdepth 1 ! -path {q(work)} -exec chmod go-rwx {{}} +",
            f"setfacl -m g:lab-readers:x {q(home)}",
            f"setfacl -R -m g:lab-readers:rX {q(work)}",
            f"setfacl -R -d -m g:lab-readers:rX {q(work)}",
        ]
    lines.append('echo "  lab-readers: ${members#,} (they may need to log in again)"')
    return lines


def run_units_script(run_units, workdirs=None):
    """Workers' long runs (worker/runs.py): a unit template per worker, and the sudo rule
    that lets each worker's service start and stop its own runs."""
    from .worker.runs import sudo_rule, unit_template
    lines = ['say "long runs"']
    for aid, user in sorted(run_units.items()):
        lines.append(f"cat > /etc/systemd/system/lab-run-{aid}@.service <<'UNIT'\n"
                     f"{unit_template(aid, user, workdir=(workdirs or {}).get(aid))}UNIT")
    rules = "".join(sudo_rule(user) for _, user in sorted(run_units.items()))
    lines += ["install -d /usr/local/libexec/agentic-lab",
              "install -m 755 /opt/agentic-lab/src/deploy/worker/lab-run-ctl "
              "/usr/local/libexec/agentic-lab/",
              f"cat > /etc/sudoers.d/agentic-lab-runs.new <<'SUDO'\n{rules}SUDO",
              "chmod 440 /etc/sudoers.d/agentic-lab-runs.new",
              "visudo -cqf /etc/sudoers.d/agentic-lab-runs.new && mv "
              "/etc/sudoers.d/agentic-lab-runs.new /etc/sudoers.d/agentic-lab-runs || { "
              "rm -f /etc/sudoers.d/agentic-lab-runs.new; echo '  sudo rule for runs refused "
              "by visudo: workers cannot start long runs'; }"]
    return lines


def self_admin_script(sa):
    """The secretary host: what lets the secretary deploy main when an admin asks in Slack
    (selfadmin.py): its one sudo rule, and its deploy key and clone of the config repo
    (unless it has a GitHub identity: then its service clones over HTTPS as that identity)."""
    u = sa["config_owner"]
    q = shlex.quote
    rule = [
        'say "self-administration (deploying main from Slack)"',
        'install -m 644 "$B/apply-main.yaml" /etc/agentic-lab/apply-main.yaml',
        f"echo '{u} ALL=(root) NOPASSWD: /usr/local/bin/lab apply-main' "
        "> /etc/sudoers.d/agentic-lab.new",
        "chmod 440 /etc/sudoers.d/agentic-lab.new",
        "visudo -cqf /etc/sudoers.d/agentic-lab.new && mv /etc/sudoers.d/agentic-lab.new "
        "/etc/sudoers.d/agentic-lab || { rm -f /etc/sudoers.d/agentic-lab.new; "
        "echo '  sudo rule refused by visudo'; }",
    ]
    if sa.get("github"):        # its GitHub identity: HTTPS, its service clones (no deploy key)
        return rule + [f"echo '  config repo: the secretary uses its GitHub identity "
                       f"{sa['github']} (HTTPS, its service clones; no deploy key)'"]
    ssh = ("ssh -i ~/.config/agentic-lab/config-repo-key -o IdentitiesOnly=yes "
           "-o StrictHostKeyChecking=accept-new")
    return rule + [
        f"sudo -u {u} -H bash -c 'umask 077; mkdir -p ~/.config/agentic-lab "
        "~/.local/state/agentic-lab; test -f ~/.config/agentic-lab/config-repo-key || "
        f"ssh-keygen -q -t ed25519 -N \"\" -C {u}@agentic-lab "
        "-f ~/.config/agentic-lab/config-repo-key'",
        f"sudo -u {u} -H bash -c {q('test -d ~/.local/state/agentic-lab/config-repo/.git || '
                                    f'GIT_SSH_COMMAND={q(ssh)} git clone -q '
                                    f'{q(sa["config_url"])} ~/.local/state/agentic-lab/config-repo'
                                    ' 2>/dev/null')} "
        f"&& sudo -u {u} -H git -C ~{u}/.local/state/agentic-lab/config-repo config "
        f"core.sshCommand {q(ssh)} && echo '  config repo: cloned for the secretary' || {{ "
        "echo '  the secretary cannot reach the config repo yet. Add this deploy key to it, "
        "WITH write access (it pushes branches, never merges), then sudo lab apply again:'; "
        f"sed 's/^/    /' ~{u}/.config/agentic-lab/config-repo-key.pub; }}",
    ]


def bundle(plan, lab_path, source, out):
    """Write the host's bundle into directory `out` (mode 700)."""
    os.makedirs(os.path.join(out, "files"), mode=0o700)
    os.makedirs(os.path.join(out, "envs"), mode=0o700)
    with tarfile.open(os.path.join(out, "source.tgz"), "w:gz") as t:
        t.add(source, arcname=".", filter=lambda ti: None if any(
            p in ti.name.split("/") for p in (".git", ".venv", "__pycache__", ".pytest_cache"))
            else ti)
    shutil.copy(lab_path, os.path.join(out, "lab.yaml"))
    with open(os.path.join(out, "applied.yaml"), "w") as f:
        yaml.safe_dump(plan.get("applied") or {}, f, sort_keys=False)
    if plan.get("self_admin"):
        with open(os.path.join(out, "apply-main.yaml"), "w") as f:
            yaml.safe_dump(plan["self_admin"], f, sort_keys=False)
    for i, (path, (_, _, content)) in enumerate(sorted(plan["files"].items())):
        p = os.path.join(out, "files", str(i))
        with open(p, "w") as f:
            f.write(content)
        os.chmod(p, 0o600)
    for i, path in enumerate(sorted(plan["envs"])):
        with open(os.path.join(out, "envs", str(i)), "w") as f:
            f.write(plan["envs"][path])
    with open(os.path.join(out, "apply.sh"), "w") as f:
        f.write(script(plan))
    os.chmod(os.path.join(out, "apply.sh"), 0o700)


def plan_all(lab_path, hosts=None, tokens=None, intake=intake_secret):
    lab = load_lab(lab_path)
    tokens = tokens or Tokens()
    chosen = hosts or list(lab.hosts)
    return lab, tokens, [host_plan(lab, h, tokens, intake) for h in chosen]


def has_work(plan):
    """Does the lab install anything on this host? (A host that's only powered, with no
    agents on it, needs nothing, and may well be stopped.)"""
    return bool(plan["users"] or plan["units"] or plan["files"])


def describe(plan):
    out = [f"host {plan['host']} ({plan['name']})"]
    out.append("  users:    " + ", ".join(plan["users"]))
    out.append("  services: " + ", ".join(u for _, u in plan["units"]))
    out.append(f"  files:    {len(plan['files'])} secret/config files, {len(plan['envs'])} env files")
    for m in plan["missing"]:
        out.append(f"  ✗ missing: {m}")
    for m in plan.get("later") or []:
        out.append(f"  · not yet (optional): {m}")
    return "\n".join(out)


def run_bundle(plan, local, admin):
    """Run a host's bundle: locally (this host) or over SSH as the admin."""
    with tempfile.TemporaryDirectory(prefix="lab-apply-") as tmp:
        b = os.path.join(tmp, "bundle")
        bundle(plan, plan["lab_path"], plan["source"], b)
        if local:
            return subprocess.run(["bash", os.path.join(b, "apply.sh")]).returncode
        target = f"{plan.get('ssh_user') or admin}@{plan['name']}"
        remote = f"/tmp/lab-apply-{pysecrets.token_hex(4)}"
        as_admin = ["sudo", "-u", admin] if os.geteuid() == 0 and admin != "root" else []
        if as_admin:        # hand the staging folder to the admin only (it stays mode 700)
            pw = pwd.getpwnam(admin)
            for root, dirs, fnames in os.walk(tmp):
                for n in [root] + [os.path.join(root, x) for x in dirs + fnames]:
                    os.chown(n, pw.pw_uid, pw.pw_gid)
        # a stopped VM (the usual state of an idle GPU host) answers nothing: skip it fast
        probe = subprocess.run(as_admin + ["ssh", *SSH_OPTS, target, "true"],
                               capture_output=True, text=True)
        if probe.returncode == 255:
            if any(s in (probe.stderr or "").lower() for s in _NO_ANSWER):
                return UNREACHABLE
            print(f"  ssh to {target} failed: {(probe.stderr or '').strip()[-400:]}")
            return SSH_FAILED
        r = subprocess.run(as_admin + ["scp", *SSH_OPTS, "-q", "-r", b, f"{target}:{remote}"])
        if r.returncode:
            return r.returncode
        return subprocess.run(as_admin + ["ssh", *SSH_OPTS, target,
                                          f"sudo bash {remote}/apply.sh; rc=$?; "
                                          f"rm -rf {remote}; exit $rc"]).returncode
