# Installing an Agentic Lab (draft)

The basic steps from nothing to a working lab. The admin's Claude Code on the secretary
host walks the admin through each one (the `lab-admin` skill, DESIGN.md §11a). Each step
says **who** does it:

- **Admin**: the human who runs the lab's infrastructure.
- **Member**: any human in the team.
- **Claude**: the admin's own Claude Code session on the secretary host.
- **Secretary**: the lab's 24/7 secretary agent, once it is running.

Status marks: ✅ exists today · 🚧 partly (manual scripts) · ⏳ planned (milestone).

---

## 0. Accounts (Admin, once)

- A **Tailscale** tailnet. The admin's and members' computers join it.
- A **cloud account** (Nebius) with GPU quota, and an API key.
- A **Slack workspace** where the admin can create apps.
- A **Claude seat** (Team or Max) for each member whose agents will run.
- Optional: a **W&B** account.

## 1. The secretary host (Admin, once)

An always-on machine (on-prem box or small VM), Ubuntu 24.04 or newer.

1. Join it to the tailnet.
2. Install Claude Code and log in with the admin's own account.
3. Clone this repo and open Claude Code in it. From here on, Claude guides.

## 2. Bootstrap the lab (Claude, with the Admin)

1. ⏳ `lab init`: write the first `lab.yaml` from a few questions (lab name, tailnet, the
   admin as first member).
2. 🚧 `lab apply secretary`: install the framework (`/opt/agentic-lab`), create the
   `lab-secretary` user, start its services: the secret intake ✅, the secretary agent
   and web UI ⏳ (M4).
3. ⏳ `lab secret set cloud-api-key`: the admin types the cloud key, hidden. Only the
   secretary holds it.

## 3. Members (Admin adds, each Member acts once a year)

1. ✅ The admin adds the member to `lab.yaml` (name, Slack user id).
2. ✅ The secretary sends the member a Slack DM with a one-time code and one command line.
   The member runs it on their own computer: `lab-member.py claude-token`, which runs
   `claude setup-token` and sends the token straight to the lab, never through chat.
3. The secretary asks again before the token expires (one year).

## 4. Managers' Slack identities (Admin, per manager)

1. ⏳ Create a Slack app from the manifest in the repo; Claude walks through the clicks.
2. ⏳ `lab secret set slack/<manager>`: the admin pastes the app's two tokens, hidden.

## 5. Hosts (Admin + Claude, per host)

1. Create the VM: in the cloud console today; ⏳ by the secretary later.
2. Join it to the tailnet, and let the secretary host's admin SSH key in.
3. 🚧 `lab apply <host>`: base packages, the sandbox fix-ups (AppArmor, GPU wrapper,
   managed settings ✅ as `setup-sandbox.sh`), the framework.

## 6. Agents (Claude, per agent; no human steps)

🚧 `lab apply <agent>`:
- create its Unix user `lab-<agent-id>` ✅ (`create-agent-user.sh`);
- give it its human's Claude token, from the intake ✅ (manual copy today);
- generate the manager-to-worker tokens, so no human ever handles them ⏳;
- install and start its service ✅ (`lab-worker@.service` for workers; managers ⏳ M3).

## 7. Check (Claude)

1. ⏳ `lab doctor`: every service up, sandboxes start, GPU visible, tokens valid, Slack
   reachable.
2. ⏳ A first task end to end: a member asks their manager in Slack, a worker runs it, the
   result comes back in the thread.

## Maintenance

The same commands, again: edit `lab.yaml` → `lab check` → `lab plan` → `lab apply`.

- **Add an agent:** add it to `lab.yaml`, then `lab apply <agent>`.
- **Add a member:** add them to `lab.yaml`; the secretary DMs them for their token.
- **Remove an agent:** remove it from `lab.yaml`; `lab apply` stops its service and,
  after the admin confirms, deletes its user (offering to archive its work folder).
- **Renew a token:** the secretary DMs the member when it's due; it's step 3 again.
