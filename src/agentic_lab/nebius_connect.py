"""`lab nebius connect`: give the secretary VM power, with one browser login by the admin.

Run on the secretary host with sudo, after the admin has logged the Nebius CLI in once
(`nebius profile create lab-admin --endpoint api.nebius.cloud --federation-endpoint
auth.nebius.com --no-browser`, then open the printed link). It then, with the admin's login:

  1. creates a service account `lab-power` in the project, and a group `lab-power-operators`
     holding the role `compute.instance-power-operator` (view, start and stop VMs, nothing
     else), with the service account in it;
  2. generates the service account's key pair ON THIS HOST, as the secretary's user (the
     private key never leaves it), registers the public key, and sets up the secretary's
     CLI profile `lab`;
  3. checks the secretary can list the project's VMs, and prints them, so the admin can
     say which ones lab.yaml should list.
Each step looks first, so running it again is safe. The admin's own login can then be
removed (`--forget-admin`).
"""
import json
import os
import pwd
import shutil
import subprocess

SA_NAME = "lab-power"
GROUP_NAME = "lab-power-operators"
ROLE = "compute.instance-power-operator"
ADMIN_PROFILE = "lab-admin"


class ConnectError(RuntimeError):
    pass


def _nebius_bin(user):
    home = pwd.getpwnam(user).pw_dir
    for p in (os.path.join(home, ".nebius/bin/nebius"), "/usr/local/bin/nebius",
              shutil.which("nebius") or ""):
        if p and os.path.exists(p):
            return p
    raise ConnectError("the Nebius CLI isn't installed: curl -sSL "
                       "https://artifacts.nebius.cloud/cli/install.sh | bash")


def run_as(user, *args, check=True):
    r = subprocess.run(["sudo", "-u", user, "-H", *args], capture_output=True, text=True,
                       timeout=180)
    if check and r.returncode:
        raise ConnectError(f"{' '.join(args[:4])}…: {(r.stderr or r.stdout)[-400:]}")
    return r.stdout


def neb(user, *args, profile=None, check=True):
    cli = _nebius_bin(user)
    extra = ["--profile", profile] if profile else []
    return run_as(user, cli, *args, *extra, check=check)


def _id(out):
    try:
        return json.loads(out)["metadata"]["id"]
    except (ValueError, KeyError, TypeError):
        raise ConnectError(f"unexpected answer: {out[:200]}") from None


def connect(admin, secretary_user, project, forget_admin=False, say=print):
    p = dict(profile=ADMIN_PROFILE)
    if ADMIN_PROFILE not in neb(admin, "profile", "list", check=False):
        raise ConnectError(
            f"log the Nebius CLI in first, as {admin}: nebius profile create {ADMIN_PROFILE} "
            "--endpoint api.nebius.cloud --federation-endpoint auth.nebius.com --no-browser "
            f"--parent-id {project}   (open the link it prints)")

    say("1. service account and permission")
    got = neb(admin, "iam", "service-account", "get-by-name", "--parent-id", project,
              "--name", SA_NAME, "--format", "json", check=False, **p)
    sa = _id(got) if got.strip().startswith("{") else _id(neb(
        admin, "iam", "service-account", "create", "--parent-id", project, "--name", SA_NAME,
        "--format", "json", **p))
    say(f"   service account {SA_NAME}: {sa}")
    got = neb(admin, "iam", "group", "get-by-name", "--parent-id", project, "--name",
              GROUP_NAME, "--format", "json", check=False, **p)
    group = _id(got) if got.strip().startswith("{") else _id(neb(
        admin, "iam", "group", "create", "--parent-id", project, "--name", GROUP_NAME,
        "--format", "json", **p))
    permits = neb(admin, "iam", "access-permit", "list", "--parent-id", group, "--format",
                  "json", check=False, **p)
    if ROLE not in permits:
        neb(admin, "iam", "access-permit", "create", "--parent-id", group, "--resource-id",
            project, "--role", ROLE, **p)
    members = neb(admin, "iam", "group-membership", "list-members", "--parent-id", group,
                  "--format", "json", check=False, **p)
    if sa not in members:
        neb(admin, "iam", "group-membership", "create", "--parent-id", group, "--member-id", sa,
            **p)
    say(f"   group {GROUP_NAME} with role {ROLE}: {group}")

    say("2. the secretary's key (generated here) and CLI profile")
    try:
        _nebius_bin(secretary_user)
    except ConnectError:               # its own CLI, as lab apply installs it
        run_as(secretary_user, "bash", "-c", "curl -sSL https://artifacts.nebius.cloud/cli/"
               "install.sh | SHELL=/bin/sh bash >/dev/null")
    home = pwd.getpwnam(secretary_user).pw_dir
    key = os.path.join(home, ".nebius", "lab-key.pem")
    pub = key.replace(".pem", ".pub.pem")
    if not os.path.exists(key):
        run_as(secretary_user, "bash", "-c", f"umask 077; mkdir -p ~/.nebius; "
               f"openssl genrsa -out {key} 4096 2>/dev/null; openssl rsa -in {key} -pubout "
               f"-out {pub} 2>/dev/null")
    with open(pub) as f:
        pub_pem = f.read()
    key_id = _id(neb(admin, "iam", "auth-public-key", "create", "--parent-id", project,
                     "--account-service-account-id", sa, "--data", pub_pem, "--format",
                     "json", **p))
    neb(secretary_user, "profile", "delete", "lab", check=False)
    neb(secretary_user, "profile", "create", "lab", "--endpoint", "api.nebius.cloud",
        "--parent-id", project, "--service-account-id", sa, "--public-key-id", key_id,
        "--private-key-file-path", key)
    say(f"   key {key_id}; profile 'lab' for {secretary_user}")

    say("3. check: the VMs the secretary can see")
    vms = json.loads(neb(secretary_user, "compute", "instance", "list", "--parent-id", project,
                         "--all", "--format", "json", profile="lab") or "{}")
    for vm in vms.get("items", []):
        say(f"   {vm['metadata']['id']}  {vm['metadata'].get('name', '')}  "
            f"{(vm.get('status') or {}).get('state', '')}")
    if forget_admin:
        neb(admin, "profile", "delete", ADMIN_PROFILE, check=False)
        say(f"   removed {admin}'s own Nebius login (profile {ADMIN_PROFILE})")
    say("done: list the VMs the lab may power in lab.yaml (hosts: kind cloud, provider "
        "nebius, instance <id>), then `sudo lab apply`")
    return vms.get("items", [])
