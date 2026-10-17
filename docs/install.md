# Installing an Agentic Lab

Three steps by hand, then Claude guides the rest: the admin's own Claude Code session on
the secretary host, following the `lab-admin` skill (DESIGN.md §11a). Each step says who
acts:

- **Admin**: the human who runs the lab's infrastructure.
- **Member**: any human in the team.
- **Claude**: the admin's Claude Code session on the secretary host, in this repo.
- **Secretary**: the lab's 24/7 secretary agent, once it runs.

**The install has one human: the admin.** The lab starts with the admin as its only
member. Other members join afterwards, one at a time ("Add a member" below).

**Accounts the admin needs:** a Tailscale tailnet; a Nebius account with GPU quota; a
Slack workspace where they can create apps; a Claude seat (Team or Max) for each member
whose agents will run; optionally W&B.

Commands below run on the secretary host from the repo checkout (`~/agentic-lab`), with
the framework installed there for the admin: `uv venv && uv pip install -e .`, so `lab`
means `~/agentic-lab/.venv/bin/lab`. `sudo` commands need the full path:
`sudo ~/agentic-lab/.venv/bin/lab ...`.

---

## By hand

1. **Allocate the secretary VM** (Admin). Small and always on, Ubuntu 24.04 or newer, on
   the tailnet. An on-prem box or a small cloud VM.
2. **Install Claude Code** (Admin) and log in with the admin's own account.
3. **Give Claude the first message** (Admin):
   > Set up an Agentic Lab on this machine: https://github.com/anatoli-starostin/agentic-lab

   Claude clones the repo, reads `BOOTSTRAP.md`, and takes over. (While the repository is
   private, Claude first asks the admin to add a read-only deploy key for this machine.)

## Guided by Claude

4. **The lab's description** (Claude asks, Admin answers)
   - Claude asks for the lab's name, the admin's member id and name, and Slack member id
     (Slack: profile → ⋯ → Copy member ID), the admin's manager's id, and the first
     workers (`id@host`, e.g. `worker-a@gpu-a`), then runs:
     `lab init lab.yaml --name ... --admin ... --admin-name ... --slack-user ... --manager mgr-<admin> --worker worker-a@gpu-a`
   - `lab init` marks the admin `admin: true` and fills in their Tailscale login (for the
     web UI's logs) from this machine's owner. A separate VM for managers:
     `--manager-host mgr-vm`. Optionally add an `announcements` channel. `lab check
     lab.yaml` must pass.
   - `lab.yaml` goes into git (it holds no secrets).

5. **The secretary's own services** (Claude)
   - `sudo lab apply --host <secretary host> --allow-missing`: installs the framework in
     `/opt/agentic-lab`, creates the users `lab-secretary` and `lab-broker`, starts the
     secret intake and the secretary (state + web UI on `http://<secretary host>:8774/`).
     Services whose secrets don't exist yet (the broker's and manager's Slack apps, Claude
     tokens) are installed but held back, and listed; the next steps provide them, and the
     final `lab apply` starts them.

6. **Slack apps** (Admin creates them, Claude guides; ~2 minutes each)
   - Three apps: **Lab Approvals** (the broker's), **the secretary**, and **the admin's
     manager**. For each, Claude prints the manifest
     (`lab slack-manifest approvals | secretary | mgr-<admin>`) and a one-time link:
     `sudo -u lab-secretary lab secret request service:<broker|secretary|mgr-...> slack-app --url http://<secretary host>:8771`
   - Admin: api.slack.com/apps → Create New App → From a manifest → paste → Install →
     copy the Bot token (`xoxb-`); Basic Information → App-Level Tokens → Generate with
     `connections:write` (`xapp-`); paste both in the link. Nothing passes through chat.

7. **The admin's Claude token** (Admin, once a year)
   - `sudo -u lab-secretary lab secret request <admin> claude-token --url http://<secretary host>:8771`
     prints a one-time command. The admin runs it on their own computer: it runs
     `claude setup-token` and sends the token straight to the lab.

8. **VM power** (Admin logs in once, Claude does the rest)
   - Install the Nebius CLI for the admin, then one browser login:
     `nebius profile create lab-admin --endpoint api.nebius.cloud --federation-endpoint auth.nebius.com --no-browser --parent-id <project-id>`
     (open the printed link; on a remote host, forward its local port with `ssh -L`).
   - `sudo lab nebius connect --project <project-id> --forget-admin`: a service account
     that may only view, start and stop VMs (`compute.instance-power-operator`), its key
     generated on this host, the secretary's CLI profile; it lists the project's VMs.
   - Claude adds the GPU VMs to `lab.yaml` (`kind: cloud`, `provider: nebius`,
     `instance: <id>`, `power: on_demand`, `idle_stop_min: 30`, `tailnet_name`).
   - Fallback, an existing service account: `... secret request service:secretary nebius-sa`
     (a form with the service account id, key id and private key PEM).

9. **GPU hosts** (Admin + Claude, per host)
   - Create the VM (Nebius console, Ubuntu 24.04 with NVIDIA drivers), join it to the
     tailnet, and let the admin SSH in with passwordless sudo.

10. **Everything else** (Claude)
    - `sudo lab plan` (nothing missing?), then `sudo lab apply`: every host gets the
      framework, the sandbox setup (AppArmor, GPU passthrough), its agents' users,
      secrets and services. The tokens agents use to reach each other are generated and
      never shown.
    - `sudo lab doctor` on each host: every check passes, or says how to fix it.

11. **First task** (Admin)
    - DM the manager: *"Run a quick GPU smoke test on worker-a and show me a loss
      curve."* Progress, the plot and a summary come back in Slack; any approval comes as a
      DM from Lab Approvals. The web UI shows it all.

## Afterwards

- **The secretary** keeps the lab running: machine states and idle stops, token-expiry
  notices, usage per account, answers about the lab in Slack.
- **The admin's Claude** makes every change the same way: edit `lab.yaml` →
  `lab check` → `sudo lab plan` → `sudo lab apply`.
  - **Add a member:** add them to `lab.yaml` (name, Slack id). At that moment, send them
    their Claude-token command (step 7, for them). Then add their manager (and workers):
    its Slack app (step 6), `sudo lab apply`.
  - **Add a worker:** create the VM and join the tailnet (step 9), add it to `lab.yaml`,
    `sudo lab apply`.
  - **Remove an agent:** remove it from `lab.yaml`, `sudo lab apply`; on its host,
    `sudo systemctl disable --now lab-<role>@<id>` and, after the admin confirms,
    `sudo userdel -r lab-<id>` (offer to archive its work folder first).
- **Watching and debugging:** the web UI (`:8774`, logs for admins); `sudo lab logs
  <agent>`; `sudo lab debug <agent>` (the agent's own session, in the classic console);
  `lab manager ask` / `lab secretary ask` (talk without Slack).
