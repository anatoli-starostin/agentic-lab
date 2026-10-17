"""`lab init`: write a lab's first lab.yaml (DESIGN.md §11a, install step 6).

Driven by flags, so the admin's Claude can ask the questions in chat and run it; anything
not given is asked for when a terminal is attached. The lab starts with one member, the
admin, a secretary on this machine, and optionally the admin's manager and first workers
(each `id@host[=tailnet-name]`). Detects the tailnet and this machine's tailnet name.
"""
import json
import os
import re
import subprocess
import sys

import yaml

ID = re.compile(r"^[a-z][a-z0-9-]*$")


def tailscale_self():
    """(tailnet suffix, this machine's MagicDNS name), or (None, None)."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                           timeout=10)
        d = json.loads(r.stdout)
        name = d.get("Self", {}).get("DNSName", "").rstrip(".")
        return d.get("MagicDNSSuffix"), name or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def tailscale_login():
    """The login of whoever owns this machine on the tailnet (the admin, at install)."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                           timeout=10)
        d = json.loads(r.stdout)
        uid = str(d.get("Self", {}).get("UserID"))
        return (d.get("User") or {}).get(uid, {}).get("LoginName")
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def _ask(prompt, default=None, check=None):
    if not sys.stdin.isatty():
        if default is None:
            raise SystemExit(f"lab init: missing {prompt!r} (pass it as a flag)")
        return default
    while True:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip() or default
        if v and (check is None or check(v)):
            return v
        print("  (doesn't look right, try again)")


def parse_worker(spec, tailnet):
    """`worker-a@gpu-a` or `worker-a@gpu-a=gpu-a.example.ts.net` -> (id, host, name)."""
    m = re.fullmatch(r"([a-z][a-z0-9-]*)@([a-z][a-z0-9-]*)(?:=(\S+))?", spec)
    if not m:
        raise SystemExit(f"lab init: --worker takes id@host[=tailnet-name], got {spec!r}")
    wid, host, name = m.groups()
    return wid, host, name or (f"{host}.{tailnet}" if tailnet else None)


def build(lab_name, tailnet, admin_id, admin_name, slack_user, secretary_host,
          secretary_name, manager=None, workers=(), provider="nebius", admin_login=None,
          manager_host=None):
    hosts = {secretary_host: {"kind": "onprem", "power": "always_on"}}
    if secretary_name:
        hosts[secretary_host]["tailnet_name"] = secretary_name
    human = {"name": admin_name, "channels": [
        {"type": "slack_dm", "timeout_s": 120, "on_timeout": "deny"}]}
    if slack_user:
        human["slack_user"] = slack_user
    human["admin"] = True                 # sees agents' logs in the web UI
    if admin_login:
        human["tailscale_login"] = admin_login
    agents = {"secretary": {"role": "secretary", "human": admin_id, "host": secretary_host,
                            "model": "claude-haiku-5-5"}}
    if manager:
        mhost, mname = manager_host or (secretary_host, None)
        if mhost not in hosts:
            hosts[mhost] = {"kind": "cloud", "provider": provider, "power": "always_on"}
            if mname:
                hosts[mhost]["tailnet_name"] = mname
        agents[manager] = {"role": "manager", "human": admin_id, "host": mhost,
                           "access": "dedicated", "workdir": "~/scratch",
                           "grants": [{"tool": "WebFetch"}, {"tool": "WebSearch"}]}
    for wid, host, name in workers:
        if host not in hosts:
            hosts[host] = {"kind": "cloud", "provider": provider, "power": "on_demand",
                           "idle_stop_min": 30}
            if name:
                hosts[host]["tailnet_name"] = name
        agents[wid] = {"role": "worker", "human": admin_id, "host": host, "access": "shared",
                       "grants": [{"domain": "pypi.org"}, {"domain": "files.pythonhosted.org"}]}
    return {"lab": lab_name, "tailnet": tailnet, "humans": {admin_id: human},
            "hosts": hosts, "agents": agents, "credentials": []}


HEADER = """\
# The lab's description: the one source of truth (DESIGN.md §8). Written by `lab init`.
# Every change goes through this file: edit -> `lab check` -> `sudo lab plan` -> `sudo lab apply`.
# Keep it in git. It holds no secrets.
"""


def run(path, args):
    if os.path.exists(path) and not args.force:
        raise SystemExit(f"lab init: {path} exists (pass --force to overwrite)")
    tailnet, me = tailscale_self()
    lab_name = args.name or _ask("Lab name (e.g. deepspike)", check=ID.match)
    tailnet = args.tailnet or tailnet or _ask("Tailnet DNS suffix (e.g. example.ts.net)")
    admin_id = args.admin or _ask("Your member id (lowercase, e.g. anatoly)", check=ID.match)
    admin_name = args.admin_name or _ask("Your name", admin_id.capitalize())
    slack_user = args.slack_user or (None if not sys.stdin.isatty() else
                                     _ask("Your Slack member id (U…; Enter to add later)", "")
                                     or None)
    sec_host = args.secretary_host or (me.split(".")[0] if me else None) or \
        _ask("This machine's host id", check=ID.match)
    sec_name = args.secretary_name or me
    workers = [parse_worker(w, tailnet) for w in args.worker or []]
    mhost = None
    if args.manager_host:
        _, h, n = parse_worker(f"x@{args.manager_host}", tailnet)
        mhost = (h, n)
    data = build(lab_name, tailnet, admin_id, admin_name, slack_user, sec_host, sec_name,
                 manager=args.manager, workers=workers,
                 admin_login=args.tailscale_login or tailscale_login(), manager_host=mhost)
    with open(path, "w") as f:
        f.write(HEADER)
        yaml.safe_dump(data, f, sort_keys=False)
    return data
