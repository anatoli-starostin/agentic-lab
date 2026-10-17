# Installing an Agentic Lab

Three steps by hand, then Claude guides the rest. The admin's Claude Code on the secretary
host does the guiding (the `lab-admin` skill, DESIGN.md §11a). Each step says who acts:

- **Admin**: the human who runs the lab's infrastructure.
- **Member**: any human in the team.
- **Claude**: the admin's own Claude Code session on the secretary host.
- **Secretary**: the lab's 24/7 secretary agent, from step 4 on.

Status marks: ✅ exists · 🚧 partly (manual scripts) · ⏳ planned.

**The install has one human: the admin.** The lab starts with the admin as its only
member (on Slack and in `lab.yaml`). Other members join afterwards, one at a time, with
"Add a member" below.

**Accounts the admin needs:** a Tailscale tailnet, a cloud account (Nebius) with GPU
quota, a Slack workspace where they can create apps, a Claude seat (Team or Max) for each
member whose agents will run, and optionally W&B.

---

## By hand

1. **Allocate the secretary VM** (Admin). An always-on, small machine, Ubuntu 24.04 or
   newer: an on-prem box or a small cloud VM.
2. **Install Claude Code** (Admin) and log in with the admin's own account.
3. **Give Claude the first message** (Admin):
   > Set up an Agentic Lab on this machine: https://github.com/anatoli-starostin/agentic-lab

   Claude clones the repo, reads `BOOTSTRAP.md`, and takes over.
   (While the repo is private, this machine needs GitHub access to clone it.)

## Guided by Claude

4. **Secretary on Slack** (Claude, Admin) ⏳
   - Claude installs the framework (`/opt/agentic-lab`) and the secretary's services, under
     its own user `lab-secretary`.
   - Claude walks the admin through creating the secretary's Slack app from the manifest
     in the repo, and installing it to the workspace.
   - The admin enters the app's tokens hidden (`lab secret set`), never in the chat.
   - Check: the admin DMs the secretary and it answers. From here on, the secretary can
     reach anyone in the team.
5. **Tailnet** (Claude, Admin) ⏳
   - Claude joins this machine to the tailnet if it isn't already.
   - The admin creates one **reusable, tagged Tailscale auth key** and enters it hidden.
     Every later host then joins the tailnet without a human login.
6. **Describe the lab** (Claude, Admin) ⏳ `lab init`
   - Claude and the admin write `lab.yaml` together: lab name, tailnet, the admin as the
     only member, and the admin's first manager and workers. `lab check` ✅ validates it.
7. **The admin's Claude token** (Secretary, Admin) ✅
   - The admin is added the same way every member is (see "Add a member"): the secretary
     DMs them a one-time command (`client/lab-member.py`). They run it on their own
     computer (or right here): it runs `claude setup-token` and sends the token straight
     to the lab, never through chat. The admin's agents run on this token.
8. **Cloud access** (Admin) ⏳
   - The admin enters the cloud API key hidden. Only the secretary holds it.
9. **Hosts** (Secretary, Claude) 🚧
   - The secretary creates the manager VM and the GPU VMs through the cloud API (or the
     admin creates them in the console). They join the tailnet with the auth key.
   - `lab apply <host>`: base packages, the sandbox fix-ups (✅ `setup-sandbox.sh`), the
     framework.
10. **Managers** (Claude, Admin) ⏳
    - Each manager needs its own Slack app. With a Slack *app configuration token* (the
      admin creates one once), Claude creates each manager's app through Slack's manifest
      API; the admin only clicks "Install" once per manager.
    - `lab apply <manager>`.
11. **Workers** (Claude) 🚧 `lab apply <worker>`: no human steps.
    - Its Unix user (✅ `create-agent-user.sh`), its human's Claude token from step 7, the
      tokens managers use to reach it (generated, never handled by a human), its service
      (✅ `lab-worker@.service`).
12. **Check** (Claude) ⏳
    - `lab doctor`: every service up, sandboxes start, GPU visible, tokens valid, Slack
      reachable. The web UI shows the whole lab.
    - A first task: a member asks their manager in Slack; a worker runs it; the result
      comes back in the thread.

## Afterwards

- **The secretary** keeps the lab running: monitoring, VM power, and token renewals (it
  DMs members when their token is due).
- **The admin's Claude** makes changes, always the same way: edit `lab.yaml` →
  `lab check` → `lab plan` → `lab apply`.
  - **Add a member:** add them to `lab.yaml` (the secretary finds their Slack id by name
    or email). **At that moment** the secretary DMs them the one-time `lab-member.py`
    command for their Claude token; they run it once. When the token has arrived, their
    agents (a manager, maybe workers) can be added. Their token renews once a year: the
    secretary DMs them again before it expires.
  - **Add an agent / a host:** add it to `lab.yaml`, `lab apply`.
  - **Remove an agent:** remove it from `lab.yaml`; `lab apply` stops its service and,
    after the admin confirms, deletes its user (offering to archive its work folder).
