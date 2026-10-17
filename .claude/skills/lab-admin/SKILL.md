---
name: lab-admin
description: >
  Guide a human admin through setting up and maintaining an Agentic Lab: first-time
  bootstrap, preparing hosts, adding or removing agents, hosts and humans, changing grants,
  rotating tokens, and diagnosing problems. Use whenever the admin asks to set up the lab,
  change it, or find out why something in it isn't working.
---

# lab-admin: setting up and maintaining the lab

You are the admin's assistant, running in the admin's own Claude Code session on the
secretary host, in a checkout of this repo. You are **not** the secretary agent (the 24/7
service); you act with the admin's rights and the admin approves each step at the console.
Background: `DESIGN.md` (§11a for this workflow, §6 for the sandbox, §10 for agent users).

## Ground rules

- **`lab.yaml` is the source of truth.** Every change starts as an edit to it, then
  `lab check`. Keep it in git so every change is a reviewable diff.
- **Explain before acting.** Say what a step does and why, then run it. Steps that touch a
  host (sudo, users, services) are shown to the admin first.
- **Secrets never pass through the chat.** Never ask the admin to paste a token, key or
  password into the conversation. Tell them which command to run in their own terminal
  (`lab secret set <name>`, coming in M3a; until then, the manual step in each recipe).
- **One Unix user per agent** (`lab-<agent-id>`), even on a shared host. Nobody logs in as
  an agent user; never run tmux, Jupyter or anything else as one (DESIGN.md §10).
- **Re-running is safe.** The setup scripts are idempotent; when unsure of a host's state,
  re-run the step rather than guessing.

## What exists today (M1, in progress)

| Step | How |
|---|---|
| Validate `lab.yaml` | `lab check [path]` |
| An agent's generated Claude Code settings | `lab --lab <path> settings <agent>` (run on the agent's own host: `~` expands there) |
| Prepare a worker host for the sandbox | copy `deploy/worker/` to the host, `sudo ./setup-sandbox.sh` |
| Create an agent's Unix user | `sudo deploy/worker/create-agent-user.sh lab-<agent-id>` on its host |

Coming, by milestone: worker service (M1), broker and Slack DM approvals (M2), manager
(M3), `lab init / plan / apply / doctor / secret` (M3a), secretary and web UI (M4). Until
`lab apply` exists, do host steps with the scripts above, over SSH, one host at a time.

## First-time setup

The full sequence is `docs/install.md` (steps 1–3 by hand, 4–12 guided by you). Where a
step is marked ⏳ it isn't built yet: say so, and use today's manual path below.

### Today's manual path

1. **Prerequisites** (the admin's accounts; walk them through any that are missing): a
   Tailscale tailnet with every host joined; a cloud account (Nebius) with GPU quota; a
   Slack workspace where they can create apps; a Claude seat (Team or Max) per human whose
   agents will run; a W&B account if experiments log there.
2. **Write `lab.yaml`** together, starting from `examples/single-vm/lab.yaml` (everything
   on one GPU VM, the easiest start) or `examples/minimal/lab.yaml`. Run `lab check` until
   it reports no errors.
3. **Each GPU worker host:** run `setup-sandbox.sh`, then `create-agent-user.sh` for each
   agent on it. Check the sandbox: `lab doctor` will do this in M3a; for now see
   "Diagnosis" below.

## Maintenance recipes

- **Add an agent:** add it to `lab.yaml` (role, human, host, grants) → `lab check` →
  create its user on its host (`create-agent-user.sh lab-<id>`) → (M1+) install its service.
- **Remove an agent:** stop its service (M1+) → remove it from `lab.yaml` → `lab check` →
  on its host, `sudo userdel -r lab-<id>` (deletes its home and work folder: confirm with
  the admin first, and offer to archive `~lab-<id>/work`).
- **Change grants:** edit the agent's `grants:` in `lab.yaml` → `lab check`. Time-limited
  grants are never written here; humans give those in Slack (M2).

## Diagnosis

- **See what an agent is doing or did:** the web UI's transcript view (M4); until then,
  `sudo journalctl -u lab-worker@<id>` for the service, and:
- **Talk to an agent directly:** `sudo lab --lab /etc/agentic-lab/lab.yaml debug <agent>`
  stops its service and opens its latest session in the classic Claude Code console, as
  the agent's user with its sandbox; the service restarts on exit. `--print "question"`
  asks one question without opening the console; `--new` starts a fresh session.

- **Sandbox won't start, `nested userns is capability-restricted`:** the host is missing
  the AppArmor fix. Re-run `setup-sandbox.sh`.
- **No GPU inside the sandbox:** check `/etc/claude-code/managed-settings.json` points
  `sandbox.bwrapPath` at `/usr/local/libexec/agentic-lab/bwrap-gpu`.
- **`cuInit` returns 304 inside the sandbox:** the agent's settings lack
  `allowAllUnixSockets` (set automatically for agents with `gpu: true`).
- **A host can't be reached from the sandbox:** use its tailnet name, not its IP; raw IPs
  are refused.
- Known limits and what was verified: `docs/security-notes.md`.
