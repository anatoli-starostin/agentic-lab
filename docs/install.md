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
whose agents will run; a GitHub account (or organisation) for the lab's private
configuration repository.

**Two repositories:**
- **The framework** (this repository): code, read-only for the lab. Never edited on the
  secretary host.
- **The lab's configuration** (private, one per lab, e.g. `<org>/lab-config`): `lab.yaml`
  and `framework.lock`, which pins the framework to one commit. Every change to the lab is
  a commit here, so the lab's history is reviewable and a lost secretary host is rebuilt
  from it. It holds no secrets (those stay on the hosts).

On the secretary host the framework is checked out in `~/agentic-lab` (set up with
`uv venv && uv pip install -e .`) and the configuration in `~/lab-config`. Until the first
`lab apply`, `lab` means the checkout's own, `~/agentic-lab/.venv/bin/lab`, also under sudo.
`lab apply` then puts `lab` on every lab host's PATH (`/usr/local/bin/lab`), where it reads
`/etc/agentic-lab/lab.yaml` by default; that's what commands run as the lab's users use.
**Always run `lab plan` / `lab apply` from the checkout, on the config repo's lab.yaml**,
so they ship the pinned code:
`cd ~/agentic-lab && sudo .venv/bin/lab --lab ~/lab-config/lab.yaml plan`.
Below, `lab` is short for that.

**Only reviewed configuration is deployed.** `lab apply` fetches the config repo and
deploys `lab.yaml` and `framework.lock` **as they are on `main`**, never the working copy.
Every change goes: Claude edits on a branch → `lab plan --working-tree` (preview) → commit,
push → `lab pr-link` → Claude tells the admin "here is the PR, review and merge it" → once
merged, `sudo lab apply` deploys exactly that commit. Each host records which config and
framework commits it runs (`/etc/agentic-lab/applied.yaml`; shown by `lab doctor` and on the
web UI). Break glass: `sudo lab apply --unreviewed` deploys the working copy, and every host
records it as unreviewed until a reviewed apply replaces it.

**Framework pinning.** `lab apply` ships the framework checkout only if it is clean and at
the commit in `framework.lock` on `main`. To upgrade: `git -C ~/agentic-lab pull`, then
`lab apply --update-framework` (it only rewrites `framework.lock` in the working copy),
then a PR as above.

**Claude Code's auto mode** may refuse `sudo lab apply` (it changes the system) until the
admin says so explicitly in the chat: "yes, run sudo lab apply". That's expected.

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

4. **The lab's configuration repository** (Admin creates it, Claude explains and connects)
   - Claude explains the two repositories (above), then asks the admin to create an
     **empty private** repository, e.g. `https://github.com/organizations/<org>/repositories/new`
     (or `https://github.com/new`), named `lab-config`.
   - Claude generates a deploy key for it on this machine
     (`ssh-keygen -t ed25519 -N "" -f ~/.ssh/lab_config_deploy`), shows the **public**
     half, and asks the admin to add it at
     `https://github.com/<org>/lab-config/settings/keys` with **Allow write access**
     ticked. Claude clones it to `~/lab-config` with that key (`core.sshCommand` set).
   - Suggest protecting `main` (Settings → Branches: require a pull request) **after** the
     first commit is pushed (step 5), so the first push can go straight to `main`.

5. **The lab's description** (Claude asks, Admin answers)
   - **Names first.** Claude asks how each agent should be called: the secretary, the
     admin's manager, each worker. That name is what people see in Slack and in the web
     UI. Claude never invents names, and derives the ids from them (`Flash` →
     `mgr-flash`); the admin may also pick ids. Avoid naming an agent "Admin" (people
     confuse it with the human admin) or after a Slack built-in ("Slackbot").
   - Then: the lab's name, the admin's member id and name, their Slack member id (Slack:
     profile → ⋯ → Copy member ID), and (optional, else later from Slack) the admin's
     manager and the first workers' hosts. Claude runs:
     `lab init ~/lab-config/lab.yaml --name ... --admin ... --admin-name ... --slack-user ... --manager mgr-flash --worker worker-a@gpu-a --display secretary=Iris --display mgr-flash=Flash --display worker-a=Bolt`
   - `lab init` marks the admin `admin: true`, fills in their Tailscale login (for the
     web UI's logs) from this machine's owner, and writes `framework.lock`. A separate VM
     for managers: `--manager-host mgr-vm`. Optionally add an `announcements` channel.
     `lab check` must pass.
   - Claude commits both files and pushes them **directly to `main`** (the one exception:
     the repo is empty and unprotected). From now on, every change is a PR.

6. **The secretary's own services** (Claude)
   - `sudo lab apply --host <secretary host> --allow-missing`: installs the framework in
     `/opt/agentic-lab`, creates the users `lab-secretary` and `lab-broker`, starts the
     secret intake and the secretary (state + web UI on `http://<secretary host>:8774/`).
     Services whose secrets don't exist yet (the broker's and manager's Slack apps, Claude
     tokens) are installed but held back, and listed; the next steps provide them, and the
     final `lab apply` starts them.

7. **Slack apps** (Admin creates them, Claude guides; ~3 minutes each)
   - Two apps at install: **Lab Approvals** (the broker's) and **the secretary**.
     Managers (the admin's own included) and workers are best added afterwards, from
     Slack (see "Afterwards"); `lab init` without `--manager`/`--worker` gives that
     minimal lab. **One at a time**: finish, verify, then the next. For each app Claude
     puts in the chat, in this order:
     1. the manifest itself, pasted in full in a code block (`lab slack-manifest
        approvals | secretary | <manager id>`; a manifest holds no secrets);
     2. the link to create it: https://api.slack.com/apps?new_app=1
     3. the one-time link for its tokens, from
        `sudo -u lab-secretary lab secret request service:<broker|secretary|manager id> slack-app --url http://<secretary host>:8771`
     4. the clicks below, as a numbered list.
   - Admin, in the browser:
     1. *Create New App* → **From a manifest** → pick the workspace → *Next*.
     2. Select the **YAML** tab, delete what's there, paste the manifest → *Next* →
        *Create*.
     3. Left menu **Install App** → *Install to <workspace>* → *Allow*. Copy the **Bot
        User OAuth Token** (`xoxb-…`).
     4. Left menu **Basic Information** → *App-Level Tokens* → *Generate Token and
        Scopes* → any name (`socket`) → *Add Scope* → `connections:write` → *Generate*.
        Copy the token (`xapp-…`).
     5. Open the one-time link, paste both tokens, *Send*. Then tell Claude "done".
        **Never paste a token into the chat.**
   - Claude checks it at once: `sudo lab slack-verify <broker|secretary|manager id>`
     asks Slack whether both tokens work and prints the app's name, workspace and App ID;
     for a manager, Claude adds `slack_app: <App ID>` to `lab.yaml` in the next PR. If
     a check fails, it says which token and how to fix it; a new one-time link replaces
     the tokens.

8. **The admin's Claude token** (Admin, once a year)
   - `sudo -u lab-secretary lab secret request <admin> claude-token --url http://<secretary host>:8771`
     prints a one-time command. The admin runs it on their own computer: it runs
     `claude setup-token` and sends the token straight to the lab, which checks it
     with Anthropic before storing it.
   - The token is a password. **Never paste it, or the terminal's output, into a chat**
     (including Claude's). If the script shows a token that doesn't look right (cut short,
     extra characters), answer `n` and paste it into the hidden prompt instead.

9. **VM power** (Admin logs in once, Claude does the rest)
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

10. **GPU hosts** (Admin + Claude, per host)
   - Create the VM (Nebius console, Ubuntu 24.04 with NVIDIA drivers), join it to the
     tailnet, and let the admin SSH in with passwordless sudo. If the image's login user
     isn't the admin's name, add `ssh_user: <user>` to the host in `lab.yaml`.

11. **Everything else** (Claude)
    - A GPU host that's stopped can't be set up: ask the secretary to power it on first
      ("power on gpu-a"), or start it in the Nebius console.
    - `sudo lab plan` (nothing missing?), then `sudo lab apply`: every host gets the
      framework, the sandbox setup (AppArmor, GPU passthrough), its agents' users,
      secrets and services. The tokens agents use to reach each other are generated and
      never shown.
    - `sudo lab doctor` on each host: every check passes, or says how to fix it.
    - **The secretary's deploy key.** The first apply prints a public key for the
      secretary: add it to the config repo (Settings → Deploy keys) **with write access**
      (it pushes branches; branch protection keeps it from `main`), then `sudo lab apply`
      once more. From then on the lab is administered from Slack (see "Afterwards").
    - The secretary's Slack app needs **Interactivity** on (the manifest has it) for the
      Deploy button.

12. **First task** (Admin)
    - DM the manager: *"Run a quick GPU smoke test on worker-a and show me a loss
      curve."* Progress, the plot and a summary come back in Slack; any approval comes as a
      DM from Lab Approvals. The web UI shows it all.

## Afterwards: the lab is administered from Slack

After the install, the admin works with **the secretary in a Slack DM**; the console is for
emergencies. The rule is the same: only what's merged to the config repo's `main` is
deployed, and the secretary checks in code that the person asking is an admin.

- **Any change** (a member, an agent, a GPU machine, grants, names, a framework upgrade):
  tell the secretary. It reads `lab.yaml` from `main`, writes the change on a branch
  (checked), and replies with the diff and "here is the PR, review and merge it". After
  the merge, tell it so: it posts a **Deploy** button; only an admin's click deploys
  (`lab apply-main`, its one sudo command). The result is posted back in the DM.
- **Add a member:** their name and Slack member ID → PR → merge → deploy; the secretary
  then DMs them their Claude-token command.
- **Add a manager:** its name → PR → merge → deploy (its service waits for its Slack
  app) → the secretary posts the app's manifest, the clicks and a one-time link for its
  tokens → you create the app (~3 minutes, in the browser) → "done" → it checks the
  tokens and offers a redeploy, which starts the manager.
- **Add a GPU worker:** first create the VM (Nebius console, Ubuntu with NVIDIA drivers),
  join it to the tailnet and give the admin SSH with passwordless sudo (step 10); then tell
  the secretary its tailnet name (and Nebius instance id for power) → PR → merge → deploy.
- **GitHub for agents (bot accounts):** create the bot's GitHub account yourself (a
  machine user: shared, e.g. `claudebot`, or one per member); add it to the org with access
  to the lab's repos only. Then tell the secretary: it adds it to `lab.yaml`
  (`github.accounts`, and `github: <bot>` on each manager and worker that uses it: its
  default identity) → PR → merge → deploy; then it posts how to make the bot's
  fine-grained token (contents, pull requests and issues of those repos) and a one-time
  link for it; then a redeploy. Agents use git and `gh` as that bot, for each piece of work
  only with their default bot, shared bots, or the requester's own. **Protect `main`** in
  each repo (pull requests only, reviews by human code owners, no bypass for bots): that
  is what keeps bots from approving or merging.
- **Remove an agent:** PR → merge → deploy. Its Unix user and work folder stay on its host
  until removed at the console (`sudo userdel -r lab-<id>`, after archiving its work).
- **From the console** (break glass, or if Slack is down): the same flow with the admin's
  Claude: a branch → `lab pr-link` → merge → `sudo lab apply`.
- **Uninstalling** (the whole lab, a host or an agent): at the console only, with the
  admin's Claude, following `docs/uninstall.md`.
- **Watching and debugging:** the web UI (`:8774`, logs for admins); `sudo lab logs
  <agent>`; `sudo lab debug <agent>` (the agent's own session, in the classic console);
  `lab manager ask` / `lab secretary ask` (talk without Slack).
