# Uninstalling an Agentic Lab

**For Claude:** the admin's own Claude Code session on the secretary host, in the framework
checkout (`~/agentic-lab`), with the admin at the console. Never from Slack: the secretary
has no tool for this, by design.

Uninstalling deletes agents' work and the lab's secrets. So:

- **Ask first, then act, one stage at a time.** Before each stage say what it removes, on
  which hosts, and wait for the admin's explicit yes. A "yes" for one stage isn't a yes for
  the next.
- **Look before deleting.** List what's actually there (users, folders, units) and show
  it; delete only what the list below names *and* you found.
- **Offer to archive** each agent's work folder before its user is removed.
- **Everything below is safe to re-run.** If a stage fails halfway, fix it and run it again.

## What to uninstall

Ask the admin which of these they want:

1. **The whole lab**: every host in `lab.yaml`, then the outside pieces (Slack, GitHub,
   Nebius). Stages A to F below, in order.
2. **One host** (e.g. a GPU VM leaving the lab): first remove its agents from `lab.yaml`
   through the usual PR (docs/install.md, "Afterwards") and deploy; then stages B to E
   on that host only.
3. **One agent**: remove it from `lab.yaml` through a PR, then deploy: the deploy stops and
   disables its service and removes its env file (`/etc/agentic-lab/<role>-<id>.env`), its
   run unit (`/etc/systemd/system/lab-run-<id>@.service`) and, if it was the host's last
   worker, the runs sudo rule. Check with `systemctl status lab-<role>@<id>` on its host,
   then stage C for its user only.

Read `lab.yaml` from the config repo's `main` for the hosts and agents
(`git -C ~/lab-config fetch && git -C ~/lab-config show origin/main:lab.yaml`). A Unix
user is `lab-<agent id>`; the secretary host also has `lab-secretary` and `lab-broker`.
Remote hosts are reached over SSH as the admin (with `ssh_user` from `lab.yaml` if set),
with passwordless sudo, as `lab apply` does.

## A. Before anything: what's running

- Ask the secretary in Slack, or look at the web UI (`http://<secretary host>:8774/`):
  tasks running or queued, approvals pending. Tell the admin; running tasks are lost.
- Powered-off GPU VMs can't be cleaned: power them on first (Nebius console, or ask
  the secretary), or skip them and say so.

## B. Stop the services (every host)

```
sudo systemctl stop 'lab-*'                       # stop takes patterns; disable doesn't
for u in $(systemctl list-unit-files 'lab-*' --no-legend | awk '{print $1}'); do
    sudo systemctl disable "$u" 2>/dev/null
done
for u in $(systemctl list-units 'lab-*' --all --plain --no-legend | awk '{print $1}'); do
    sudo systemctl disable "$u" 2>/dev/null       # instances, e.g. lab-worker@worker-a
done
systemctl list-units 'lab-*' --all --no-legend    # nothing should be left running
```
Stop the secretary host **last** (its services hold the approvals and Slack).

## C. Agents' users and their folders (every host)

```
getent passwd | awk -F: '$1 ~ /^lab-/ {print $1, $6}'    # show this list to the admin
```
For each user, after the admin's yes and the archive offer
(`sudo tar czf ~/lab-archive-<user>.tgz -C /home/<user> work`):
```
sudo pkill -u <user>; sudo userdel -r <user>
```
`-r` deletes the home folder: the agent's work, its sessions, its secrets (Claude token,
Slack tokens), its state (task queue, memory, usage, event log).

## D. The framework's files (every host)

```
sudo systemctl disable --now lab-keep-awake.timer 2>/dev/null
sudo rm -f /etc/systemd/system/lab-keep-awake.service /etc/systemd/system/lab-keep-awake.timer
sudo rm -rf /etc/agentic-lab /opt/agentic-lab /var/log/agentic-lab /var/lib/agentic-lab
sudo rm -f /usr/local/bin/lab /etc/sudoers.d/agentic-lab /etc/sudoers.d/agentic-lab-runs
sudo rm -f /usr/local/libexec/agentic-lab/lab-run-ctl  # what that sudo rule allowed
sudo rm -f /etc/systemd/system/lab-run-*@.service     # workers' long runs (stopped with lab-*)
sudo groupdel lab-readers 2>/dev/null                 # hosts with readers
sudo rm -f /etc/systemd/system/lab-worker@.service /etc/systemd/system/lab-manager@.service \
           /etc/systemd/system/lab-secretary.service /etc/systemd/system/lab-broker.service \
           /etc/systemd/system/lab-intake.service
sudo systemctl daemon-reload
```
`/etc/agentic-lab` holds the generated tokens agents use with each other
(`private/tokens.yaml`): they stop working once it's gone.

## E. The sandbox setup (every host, only if nothing else on it uses Claude Code's sandbox)

Ask: does anyone else on this machine run Claude Code with its sandbox? If yes, keep this
stage's pieces (the GPU wrapper and AppArmor profile help them too) and say so.

- `/etc/claude-code/managed-settings.json`: remove only the `sandbox.bwrapPath` entry the
  lab added (`/usr/local/libexec/agentic-lab/bwrap-gpu`); keep any other managed
  settings. If it was the only entry, delete the file. Show the file before and after.
- `sudo rm -rf /usr/local/libexec/agentic-lab`
- AppArmor (only where `setup-sandbox.sh` changed it):
  ```
  sudo apparmor_parser -R /etc/apparmor.d/bwrap 2>/dev/null; sudo rm -f /etc/apparmor.d/bwrap
  sudo rm -f /etc/apparmor.d/disable/bwrap-userns-restrict
  sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict 2>/dev/null
  ```
- Leave the packages the lab installed (`bubblewrap`, `socat`, `acl`, `gh`, `librsvg2-bin`) and
  `/usr/local/bin/uv` unless the admin asks; they're harmless and may be used.

## F. Outside the hosts (the admin, in their browsers; Claude lists it)

- **Slack**: delete the lab's apps (api.slack.com/apps): Lab Approvals, the secretary,
  every manager. Their tokens die with them.
- **Claude tokens**: each member revokes the token they gave the lab (claude.ai settings,
  Claude Code tokens). Uninstalling doesn't revoke them.
- **GitHub identities**: for each GitHub App, delete it (or uninstall it and delete its
  private keys) in the organisation's Settings → Developer settings → GitHub Apps; for each
  bot account, revoke its token (log in as the bot → Settings → Developer settings →
  Personal access tokens), and remove or keep the account.
- **GitHub**: in the config repo, remove the secretary's deploy key (or revoke its
  identity's token / uninstall its App) and the admin's;
  archive or delete the repo. In the framework repo (if private), remove this machine's
  deploy key.
- **Nebius** (if `lab nebius connect` was used): delete the service account `lab-power`,
  its key, and the group `lab-power-operators`, or keep them for a reinstall. The VMs
  themselves are untouched: stop or delete them in the Nebius console if they're no
  longer needed.
- **The admin's own files** on the secretary host, if they want: `~/agentic-lab`,
  `~/lab-config`, `~/.ssh/agentic_lab_deploy*`, `~/.ssh/lab_config_deploy*`, and the
  Nebius CLI's profile `lab-admin` (or all of `~/.nebius`).
- Tailscale is the lab's network but not the lab's: leave the machines on the tailnet
  unless the admin says otherwise.

## Finally

Run on each host: `systemctl list-units 'lab-*' --all`, `getent passwd | grep '^lab-'`,
`ls /etc/agentic-lab /opt/agentic-lab 2>&1`: nothing should be left. Tell the admin what
was removed, what was kept on purpose (and why), and what they still have to do in
their browsers (stage F).
