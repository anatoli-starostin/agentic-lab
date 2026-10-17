# Bootstrap: start here, Claude

You were asked to set up an Agentic Lab on this machine. This machine becomes the lab's
**secretary host**, and you are the **admin's assistant**: you run with the admin's rights,
and the admin approves each step at the console.

1. **Clone this repository** to `~/agentic-lab` (if you're reading this from GitHub, clone
   it first), and work from there.
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
- **`lab.yaml` is the source of truth**, kept in git. Every change goes through it.
- If a step fails, `sudo lab doctor` usually says why; fix and re-run (every step is
  safe to repeat).
