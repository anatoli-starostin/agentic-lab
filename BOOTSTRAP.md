# Bootstrap: start here, Claude

You were asked to set up an Agentic Lab on this machine. This machine becomes the lab's
**secretary host**, and you are the **admin's assistant**: you run with the admin's rights,
and the admin approves each step at the console.

There are **two repositories**: this one, the framework (read-only for you: never edit it
here), and the lab's own private **configuration repository** (`lab.yaml`,
`framework.lock`), which you create with the admin in install step 4 and push to after
every change.

1. **Clone this repository** to `~/agentic-lab` and work from there. If the clone is
   refused, the repository is private: give this machine read-only access with a deploy
   key. Generate one (`ssh-keygen -t ed25519 -N "" -f ~/.ssh/agentic_lab_deploy`), show
   the admin the **public** half (`.pub`; never the private one) and ask them to add it at
   `https://github.com/<owner>/agentic-lab/settings/keys` (*Add deploy key*, read-only).
   Then clone over SSH with
   `GIT_SSH_COMMAND="ssh -i ~/.ssh/agentic_lab_deploy -o IdentitiesOnly=yes" git clone git@github.com:<owner>/agentic-lab.git ~/agentic-lab`
   (and set `core.sshCommand` in the clone to the same, for later pulls).
2. **Read the `lab-admin` skill** (`.claude/skills/lab-admin/SKILL.md`): your ground rules
   and recipes. Claude Code also loads it automatically when started inside the repo.
3. **Install the framework for the admin** in the checkout: `uv venv && uv pip install -e .`
   (install `uv` first if it's missing), so `lab` is `~/agentic-lab/.venv/bin/lab`.
4. **Follow `docs/install.md` from step 4.** Steps 1–3 (this machine, Claude Code, the
   first message) are done. Tell the admin the plan in a few lines first: what you'll do,
   what they will need to do themselves (create a Slack app, enter tokens, run commands in
   their terminal), and roughly how long it takes.

Ground rules, in short (the skill has the rest):
- **Secrets never pass through the chat.** Never ask the admin to paste a token or key
  into the conversation. Tell them which command to run in their own terminal; it reads
  the value hidden.
- **Explain, then act.** Before a step that changes the system (sudo, new users,
  services), say what it does and why.
- **`lab.yaml` is the source of truth**, in the config repo. Every change goes through
  it, and is committed and pushed there.
- **Ask, don't assume.** Agent names, ids, hosts: ask the admin. But never ask what you
  can find out yourself (`lab slack-verify` finds a Slack app's App ID, `tailscale status`
  the hosts).
- **Slack apps one at a time**, each with its manifest pasted in the chat, clickable
  links and numbered clicks (install step 7).
- If a step fails, `sudo lab doctor` usually says why; fix and re-run (every step is
  safe to repeat).
