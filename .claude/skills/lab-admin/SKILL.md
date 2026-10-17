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

- **`lab.yaml` is the source of truth**, in the lab's private config repo (`~/lab-config`,
  next to `framework.lock`). `lab apply` deploys them **as they are on `main`**, never the
  working copy. Every change: a branch → edit → `lab check` → `sudo lab plan
  --working-tree` → commit, push → `lab pr-link` → tell the admin "here is the PR, review
  and merge it" (never merge it yourself) → after the merge, `sudo lab apply`. Only the
  very first commit goes straight to `main`. `--unreviewed` is break glass: only when the
  admin asks for it explicitly. Never edit the framework checkout (`~/agentic-lab`):
  `lab apply` refuses a dirty one, or one not at the pinned commit.
- **Ask, don't assume; derive before asking.** Names of agents (what people see in Slack)
  are the admin's choice: ask, never invent; derive ids from them. But don't ask for what
  a command can tell you (a Slack App ID: `lab slack-verify`; tailnet names: `tailscale
  status`; VM ids: `lab nebius connect`).
- **Guide clicks precisely.** For anything done in a browser, give a clickable link and
  numbered clicks, and paste what they must paste (manifests) in a code block. One thing
  at a time; wait for "done", verify, then the next.
- **Explain before acting.** Say what a step does and why, then run it. Steps that touch a
  host (sudo, users, services) are shown to the admin first.
- **Secrets never pass through the chat.** Never ask the admin to paste a token, key or
  password into the conversation. Tell them which command to run in their own terminal
  (`lab secret request ...` gives a one-time link or command). If they paste one anyway,
  tell them to revoke and regenerate it.
- **Auto mode may block `sudo lab apply`.** Explain what it will do and ask the admin to
  say "yes, run it" explicitly.
- **One Unix user per agent** (`lab-<agent-id>`), even on a shared host. Nobody logs in as
  an agent user; never run tmux, Jupyter or anything else as one (DESIGN.md §10).
- **Re-running is safe.** The setup scripts are idempotent; when unsure of a host's state,
  re-run the step rather than guessing.

## The commands

| Step | How |
|---|---|
| Write the first `lab.yaml` | `lab init ~/lab-config/lab.yaml --name ... --admin ... --admin-name ... --slack-user ... [--manager ...] [--worker id@host ...] --display AGENT=NAME ...` (also writes `framework.lock`) |
| Validate `lab.yaml` | `lab check lab.yaml` |
| What would change; missing secrets | `sudo lab --lab lab.yaml plan` |
| Make the hosts match `lab.yaml` on `main` | `sudo lab --lab ~/lab-config/lab.yaml apply [--host H]` (from the framework checkout at the pinned commit; remote hosts over SSH as the admin, who needs passwordless sudo there) |
| Is this host healthy? | `sudo lab doctor` (on any host; every failure says how to fix it) |
| A member's Claude token | `sudo -u lab-secretary lab secret request <member> claude-token --url http://<secretary>:8771` → give them the message |
| A Slack app's tokens / a Nebius service account | `sudo -u lab-secretary lab secret request service:<broker\|secretary\|manager-id> slack-app --url ...` (or `service:secretary nebius-sa`) → a one-time link to paste in a browser |
| Check a Slack app's tokens (and get its App ID) | `sudo lab --lab ~/lab-config/lab.yaml slack-verify <broker\|secretary\|manager-id>` |
| Open a PR for the current config branch | `lab --lab ~/lab-config/lab.yaml pr-link` (after `git push -u origin <branch>`) |
| Preview a branch's changes | `sudo lab --lab ~/lab-config/lab.yaml plan --working-tree` |
| What a host runs | `sudo lab doctor` (config + framework commits, reviewed or not) |
| Upgrade the framework | `git -C ~/agentic-lab pull` → `lab apply --update-framework` (rewrites `framework.lock` only) → PR |
| A Slack app manifest | `lab --lab lab.yaml slack-manifest <manager or secretary>` / `slack-manifest approvals` |
| VM power | after the admin's one Nebius browser login: `sudo lab --lab lab.yaml nebius connect --project <id> [--forget-admin]` |
| Talk to an agent without Slack | `sudo -u lab-<id> lab manager ask <id> --as <member> "..."` (or `secretary ask --as <member> "..."`) |
| An agent's log | web UI `/logs/<agent>` (admins), or `sudo lab logs <agent> [--hours N]` on its host |
| Talk to an agent in the classic console | `sudo lab debug <agent>` (`--print "q"` for one question) |
| The lab's web UI | `http://<secretary host>:8774/` on the tailnet |

## First-time setup

The full sequence is `docs/install.md`: steps 1–3 by hand, 4–12 guided by you. Follow it
in order; tell the admin before each step what they will need to do themselves.

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
- **Talk to an agent directly:** `sudo lab debug <agent>`
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
