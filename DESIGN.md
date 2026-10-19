# Agentic Lab — design

A framework that lets a team of **humans and AI agents run a scientific lab together**:
members talk to **manager** agents in Slack, managers delegate work to **worker** agents on
GPU machines, and a **secretary** agent keeps the lab running. Humans stay in control of
anything that crosses a boundary, through a permission channel that **agents cannot spoof**.

> Status (2026-10-18): implemented and on `main`; rehearsed end to end on a single GPU VM.
> `docs/architecture/architecture.pdf` is the illustrated reference; this file is the
> design with its reasoning and the decisions log (§13). Section numbers are stable: the
> code refers to them.

This document is deliberately free of hostnames, tokens and account ids. Those belong in a
deployment's own `lab.yaml` (in its private config repository) and secret files.

---

## 1. Goals and non-goals

**Goals**
- Three agent roles (**secretary**, **manager**, **worker**) with clear jobs and guardrails.
- **Frictionless inside, approval outside.** An agent does nearly all its work in a sandbox
  with no prompts. Anything that crosses the sandbox boundary needs a real human's yes.
- **Everything is visible.** A shared **web UI** (§9) shows agents, tasks, approvals, grants,
  machines and Claude usage per account. Transparency replaces spend limits and quotas.
- **Humans are never bypassed.** Agents relay approval requests; they never answer them, and
  they never approve or merge pull requests. **Code enforces this, not prompts.**
- **One description of the lab.** `lab.yaml` lists the humans, agents, hosts, grants, GitHub
  bots and expiring credentials. It lives in the lab's private config repository; only what
  is merged to its `main` is deployed (§11a).
- **Easy to set up and maintain.** The admin's own Claude Code guides the install; after it,
  admins administer the lab in a Slack DM with the secretary (§11a).
- **Portable and generic.** No hard-coded hosts, organisations or providers. Nebius, Slack
  and Tailscale come first. Project tools (experiment trackers, data stores...) are
  extensions a lab adds, not part of the framework.

**Non-goals (for now)**
- A general multi-tenant SaaS. One lab = one deployment = one tailnet.
- Replacing a project's own tools: code and results stay in git, experiment tracking in
  whatever the project uses.
- **Per-token (API) billing.** Every agent runs on a human member's Claude seat.
- Agents that approve each other. There is no agent-to-agent authority over boundary
  crossings.

---

## 2. Prerequisites

- A **Tailscale tailnet**: every host is on it; no lab service listens on a public interface.
- A **Slack workspace** where the admin can create apps: one app per Slack-facing agent (the
  secretary, each manager) plus one **Lab Approvals** app for the broker.
- A **Claude seat** (Team or Max) for each member whose agents will run.
- A **cloud account** (Nebius first) for GPU workers, and any VMs for managers.
- An **always-on host** for the secretary (small, no GPU).
- A **GitHub** account or organisation for the lab's **private config repository**, and
  optionally bot accounts for agents (§4.4).

---

## 3. Topology

```
                 ┌──────────────────────── Slack ─────────────────────────┐
   members ──────┤ one app per Slack-facing agent  +  Lab Approvals app    │
   (browsers:    └────▲──────────────────────▲──────────────────────▲─────┘
    web UI,           │ (all connect out: Socket Mode)              │
    intake)  ┌────────┴──────────── SECRETARY HOST (always on) ─────┴──────┐
       ─────►│ secretary + web UI :8774   broker :8772   intake :8771       │──► Nebius API
             │ deploys (lab apply / apply-main, over SSH)                   │──► GitHub (config repo)
             └──▲───────────▲───────────────────────────────────▲───────────┘
   monitoring   │           │ permission requests (every agent) │
   (read-only)  │   ┌───────┴────────────┐                      │
                │   │ MANAGERS (any host)│                      │
                │   │ status :8773       │                      │
                │   └───────┬────────────┘                      │
                │           │ tasks, files (MCP over the tailnet)
            ┌───┴───────────▼──┐                     ┌──────────┴─────┐
            │ GPU VM: worker   │        ...          │ GPU VM: worker │
            │ MCP :8770        │                     │ MCP :8770      │
            └──────────────────┘                     └────────────────┘
       every agent → Claude (its member's seat) · agents with a bot → GitHub
```

- **Secretary host**: always on (on-prem or a small VM). Runs the secretary, the broker,
  the intake and the web UI; deploys run from here.
- **Managers**: on any host — the secretary host, a VM of their own, or a GPU VM next to
  their workers (`host:` per manager).
- **Workers**: GPU VMs, `on_demand` (stopped when idle) or `always_on`.
- Several agents on one host each have their own Unix user (§10).

---

## 4. Roles

### 4.1 Secretary — "keeps the lab running"

| Does | Doesn't |
|---|---|
| Collect the lab's state every 15 s; serve the **web UI** (§9) and a Slack Home tab | Research or experiments (it points to the member's manager) |
| Power cloud VMs on and off; **stop idle ones** | Approve anything for anyone |
| Show Claude usage per account; warn about expiring tokens and credentials | Read the content of secrets it doesn't need |
| Answer questions about the lab in Slack (DMs, threads it's tagged in) | Power off its own host |
| Administer the lab with admins: propose changes as PRs, deploy `main` (§11a) | Merge anything |

- **State**: workers over MCP (a read-only monitoring token), managers' status endpoints,
  the broker's overview, local systemd units, machine power states (Nebius).
- **Power**: any member may power a machine on, or off when it's idle. A **busy** machine
  (a worker task running, queued or awaiting approval; any process on a GPU, anyone's; a GPU
  above 0% in any of its last 5 readings, about 75 s; someone's `~/.lab-keep-awake`; a
  manager on it) may be powered off only by an admin, after confirming. Idle `on_demand` hosts with only workers are stopped after `idle_stop_min`.
- **Keep-awake**: someone working on a machine directly (not through agents, e.g. a
  CPU-only job) creates `~/.lab-keep-awake` in their home (first line, optional: why); while
  it's there the machine is busy, and the web UI shows who keeps it awake. A root timer
  (`lab-keep-awake.timer`) lists these files every minute in `/var/lib/agentic-lab/`, since
  homes are private.
- **Notices**: daily, credentials and GitHub bot tokens expiring within 14 days; lab-wide
  notices go to an optional `announcements` channel.
- **Chat**: a Claude agent with tools over the lab's state and the power, token-request and
  admin tools; admin-only tools are checked in code.

### 4.2 Worker — "does the science"

- Runs as a **service** exposing the **worker MCP server** (§5) on its tailnet address.
- Executes tasks inside the **sandbox** (§6.1), without prompts. Anything outside it is a
  **permission request to the broker** (§6.3), for the task's requester.
- **Attached to one human for tokens** (whose seat it runs on). **Access** is separate:
  `dedicated` (only its member's managers may submit tasks) or `shared` (any manager).
- **No fixed results contract**: a result is a summary plus whatever artifacts fit (files,
  commits, links). The worker follows the project's conventions.
- **Watching and debugging** (the same for every agent): the agent's own **event log**
  (messages, tool calls, results, approvals, task events), shown to admins in the web UI;
  `sudo lab debug <agent>` stops its service and opens **the same session** in the classic
  Claude Code console, as the agent's user, with its sandbox; the service restarts on exit.

### 4.3 Manager — "the team member humans talk to"

- Its own **Slack identity** (Socket Mode). A DM is one conversation; in channels it joins
  only threads where it's mentioned or already involved, reading the thread so far on first
  mention (its first 5 messages, which often set the topic, and the last 50 before the
  mention). Only lab members are answered.
- Attached to a human **for tokens**; **access** `dedicated` (its member gets everything;
  other members are **guests**) or `shared` (every member gets everything, with a
  separate memory per member, enforced in code).
- **Guests** only talk: questions, explanations, scientific discussion. In a guest's
  conversation code removes every tool (commands, files, worker tasks, GitHub — the bot's
  token is left out — anything outside the sandbox, memory), and turns are cut off after
  2 minutes. Asked for real work, the manager points to the guest's own manager or a
  shared one.
- **Turns requests into worker tasks**, follows them and reports back (progress, plots,
  summaries) in the conversation that asked.
- **Relays approvals**: it can show what a task waits for and add a note to the request; it
  **cannot answer** one (§6.3). Its own actions outside its sandbox are permission requests
  for the member whose message started the work.
- **Stays reachable**: one turn at a time *per conversation* (other threads and members are
  never blocked); it does work itself only when it takes a few minutes; a turn longer than
  `turn_budget_min` (default 5) is cut off, and it gets one short turn to hand the rest to a
  worker; a message arriving meanwhile gets an immediate "still working" reply.
- Auto-compacts its context early (17% of the window) to keep long conversations cheap.

### 4.4 GitHub for agents

A manager or worker may have **one GitHub identity**, named in `lab.yaml` (`github: <name>`;
several agents may share one). Two kinds, side by side:
- **A GitHub App** (`app_id`): appears as `<slug>[bot]`, no account or email to create, so
  every agent can easily have its own. Tokens last an hour: the agent's service holds the
  app's private key (unreadable by the agent), mints a token, keeps a fresh one where git
  and `gh` read it (`github_app.py`), and mints the next before it expires.
- **A full bot account** (a GitHub user with its own email): a fine-grained token in the
  agent's environment (`GH_TOKEN`), its expiry tracked.

Either way git and `gh` are set up, commits carry the identity's name and email, and
`github.com` is reachable from the sandbox. The agent can read its current token: the
safety is GitHub's — private repositories, a fine-grained token limited to the lab's repos
(contents, pull requests, issues), and **branch protection** on `main` (pull requests,
reviews by human code owners, no bypass for bots). Bots push branches and open pull
requests, issues and comments; they never approve or merge.

---

## 5. The worker protocol (MCP over the tailnet)

Each worker runs an MCP server (streamable HTTP) on **its tailnet address**, port 8770. Every
request carries a bearer token: one per manager allowed on the worker (a dedicated worker
accepts only its member's managers), plus the secretary's **read-only** monitoring token.

| Tool | Purpose |
|---|---|
| `submit_task(instruction, requester, project?)` | Queue a task (first come, first served). The manager also registers the task with the broker, so the requester is known for approvals. |
| `task_status(task_id)` | `queued \| running \| awaiting_approval \| done \| failed \| cancelled`, plus the latest progress line. |
| `task_result(task_id)` | All progress lines and, when finished, the result (summary + artifacts). |
| `cancel_task(task_id)` | A queued task at once; a running one as soon as the agent can stop. |
| `list_tasks(state?)` | The queue. |
| `get_guardrails()` | The worker's sandbox settings, standing and active grants. |
| `read_file(path)`, `write_file(name, data)` | Files in its working folder (up to 20 MB): results out, attachments in. |
| `health_check()` | Queue, GPUs and the processes on them, disk, usage. |
| `check_usage()` | Read its seat's limits now (the web UI's "Check now"). |
| `recent_events(hours)` | Its event log: **the secretary's monitoring token only** (the admins' log viewer). |

Managers follow tasks by polling. The queue is durable on disk; after a restart a running
task is requeued and its Claude session resumed. One task runs at a time per worker.

---

## 6. Guardrails and the trust model

### 6.1 One simple model for every agent

| | **Inside the sandbox: auto-allowed** | **Outside the sandbox: only with permission** |
|---|---|---|
| Internet | none (plus granted hosts; github.com for an agent with a bot) | allowed after a human says yes |
| Tailnet (lab resources) | **allowed**, by tailnet name (raw IPs refused) | — |
| Local files | write the **working folder and /tmp**; read anything except `/run` and its service's secrets and state | writes elsewhere need permission |
| GPU | available to workers | — |

Enforcement is **Claude Code's built-in sandbox** (bubblewrap + seccomp + a network proxy),
rendered per agent from `lab.yaml` (`lab settings <agent>`):
- **Every Bash command runs sandboxed** and is allowed without asking. There is no command
  parsing. The sandbox protects `.git/hooks`, `.git/config` and Claude's own settings.
- `failIfUnavailable`: an agent never runs unsandboxed by accident. Strict network allowlist:
  unknown hosts are refused, not prompted.
- **GPU**: a `bwrap-gpu` wrapper (set as `sandbox.bwrapPath` in Claude Code's managed
  settings) binds the NVIDIA devices. CUDA needs Unix sockets, so GPU agents allow them and
  `/run` is hidden instead (closing a `systemd-run` escape). Host setup:
  `deploy/worker/setup-sandbox.sh` (AppArmor profile for bubblewrap, the wrapper, the
  managed settings).
- **Outside the sandbox**: Bash re-run unsandboxed, writes outside the allowed folders, other
  tools — each is a permission request (§6.3).
- **The tailnet is trusted by design for now**; this may tighten to per-host lists.

**The usual flow:** the agent runs a command → the sandbox refuses it (say, a host that isn't
allowed) → the agent sees why and retries it outside the sandbox → that's a permission
request → the human decides (§6.2).

### 6.2 Grants: permissions customised on the fly

When a human answers a request, they choose how far the answer reaches:
- **Run once**: this one action runs.
- **Allow *X* for 1 hour** or **for this task**: a **grant** that widens the sandbox (a
  domain, a writable path, or a tool) with an expiry. The agent's session restarts with
  the wider sandbox and is told what changed (a running session doesn't pick up a changed
  allowlist; resuming is seamless).
- **Deny**, or a typed reply: "no, do it this way" (the reply goes back to the agent).

A grant always widens the **sandbox**; it never means "trust unsandboxed commands for an
hour". **Standing grants** are in `lab.yaml` per agent (changed by a reviewed PR):
```yaml
agents:
  worker-a:
    grants:
      - {domain: pypi.org}
      - {domain: files.pythonhosted.org}
      - {write: ~/datasets}
  mgr-flash:
    grants:
      - {tool: WebFetch}
```
Active grants, their expiry and who gave them are shown in the web UI.

### 6.3 Approvals: the broker decides who answered

The rule "agents never approve" holds even if an agent is confused or prompt-injected,
because **approval is a code path outside every LLM**:

```
agent's service ──request──► BROKER (secretary host, user lab-broker, no LLM)
                                 ├─► managers may see it and add a note (no decision)
                                 └─► the human's DM from the "Lab Approvals" app
                                 ◄── the human's click or reply (identity from Slack)
agent's service ◄── decision ─── broker (its own token, over the tailnet)
```

- **An approval goes to the human the action is for**: a worker's task — the requester the
  manager registered (not what the worker claims); a manager's own action — the member whose
  message started the work (taken by the manager's *code* from the Slack event); no
  requester — the agent's attached human.
- Only **Lab Approvals** posts approval requests; only the broker holds its tokens. A
  decision is recorded only after a verified interaction of the asked human.
- **No timeout by default**: a request waits until its human answers. A member may set one
  (`timeout_s`); then an unanswered request is closed as denied when it runs out: silence
  counts as "no", never as "yes" (`on_timeout` can only be `deny`), and the agent is told
  it was a timeout, not a refusal. If the agent stops waiting (its task is cancelled, a
  manager's turn is cut off), the request is **withdrawn**, so nobody is asked any more.
- Grants are created only by the broker, after a verified human answer.

### 6.4 Secrets

- **Agents can't read their lab secrets.** Each agent's service keeps them in
  `~/.config/agentic-lab` (and its state in `~/.local/state/agentic-lab`): the agent's Read
  tool is denied there, its sandbox hides them, and the Claude token is stripped from
  sandboxed commands. One exception by design: a GitHub bot's token (§4.4).
- **Secrets never pass through a chat.** They arrive through the **intake**: one-time codes
  (24 h, single use, rate-limited) for web forms, and a small client that runs
  `claude setup-token` on a member's laptop and sends the token straight to the lab. The
  intake checks Claude tokens with Anthropic and GitHub tokens with GitHub.
- **Lab tokens** (manager↔worker, agent↔broker, the secretary's monitoring tokens) are
  generated by `lab apply` and never shown.
- `lab.yaml` holds no secrets: only GitHub bots' and other credentials' expiry dates.

---

## 7. Permission channels

**This version: Slack DM only**, from the **Lab Approvals** app, with buttons **Run once ·
Allow *X* · 1 hour · Allow *X* · this task · Deny**, or a typed reply in the request's
thread (a denial with a reason).

```yaml
humans:
  alice:
    slack_user: U0123ABC
    channels:
      - {type: slack_dm, on_timeout: deny}           # optional: timeout_s: 600
```
- No timeout unless `timeout_s` is set; `on_timeout` is always `deny`.
- The broker's channel interface (`ask(request) -> decision`) leaves room for other channels
  later without touching its trust rules.

---

## 8. The lab registry: `lab.yaml`

The one description every component reads, kept in the lab's private config repository:

```yaml
lab: example-lab
tailnet: example.ts.net
announcements: "#lab"                    # optional: lab-wide notices
humans:
  alice:
    name: Alice
    slack_user: U0123ABC
    admin: true                          # may read agents' logs, administer the lab
    tailscale_login: alice@example.org   # how the web UI recognises her
    channels:
      - {type: slack_dm, on_timeout: deny}
agents:
  secretary:
    role: secretary
    human: alice                         # whose Claude seat it runs on
    host: sec-01
    display_name: Iris
  mgr-flash:
    role: manager
    human: alice
    host: sec-01                         # managers run on any host
    access: dedicated
    display_name: Flash
    github: flash-bot                    # its GitHub identity (§4.4)
    turn_budget_min: 5
  worker-a:
    role: worker
    human: alice
    host: gpu-a
    access: shared
    grants:                              # standing grants (§6.2)
      - {domain: pypi.org}
hosts:
  sec-01: {kind: onprem, power: always_on}
  gpu-a:  {kind: cloud, provider: nebius, instance: computeinstance-..., power: on_demand,
           idle_stop_min: 30, tailnet_name: gpu-a.example.ts.net}
github:
  accounts:
    flash-bot: {expires: 2027-10-01}     # its token's expiry, for reminders
credentials:
  - {name: some/other-key, host: gpu-a, expires: 2027-03-01}
```

`lab check` validates all of it at once (unknown keys are errors); agent ids are lowercase,
since they become Unix user names (`lab-<id>`). Names humans see (`display_name`) are the
admin's choice; the guide never invents them.

---

## 9. Web UI — the lab's shared window

Served by the secretary on the tailnet (`:8774`), refreshed every 20 s. **Monitoring only**:
permissions are answered in Slack (§7).

- **Claude usage, by account** (each member's seat): the 5-hour and weekly windows (the
  freshest reading of any agent on that seat, with *as of when* and *from which agent*), and
  today's tokens split by agent. Percentages come from a **probe**: each agent's service
  reads the seat's utilization headers from one tiny request every 10 minutes, only while the
  seat's 5-hour window is open; **Check now** probes at once.
- **Workers**: busy or idle, GPU (utilization, memory, processes), disk, recent tasks.
- **Approvals and grants**: pending requests and who decides; active grants and expiry.
- **Machines**: power state, policy, idle since.
- **Agents**: role, seat, host, service state.
- **Deployed**: the config and framework commits every host runs (flagged if unreviewed).
- **Agents' logs** (`/logs/<agent>`): **admins only**. The request's tailnet address must not
  be a lab host; `tailscale whois` must name a person (not a tagged device) whose login is an
  admin's in `lab.yaml`. Tailscale authenticates every packet with its device's key, so the
  address can't be forged.

The main page needs no login: it's visible to whoever can reach the secretary host on the
tailnet (restrict with Tailscale ACLs if the tailnet is wider than the lab).

## 10. Agent runtime

**Every agent runs on the Claude Agent SDK as a systemd service** (`lab-worker@<id>`,
`lab-manager@<id>`, `lab-secretary`), with Claude Code's built-in sandbox. The SDK's
permission callback *is* the gate: sandboxed Bash is allowed, active grants are checked,
anything else goes to the broker.

- **One Unix user per agent**, `lab-<agent-id>`, even when several agents share a host: no
  password, no SSH keys, a `nologin` shell, lingering off, home folder private. Agents on one
  host are separated by Unix permissions. Nobody runs anything else as these users (GPU
  agents allow Unix sockets, so a socket server running as the agent's user would be a way
  out). Humans log in as themselves; `sudo lab debug <agent>` switches to the agent's user.
- Sessions persist on disk and resume after restarts.
- **Every agent is attached to a human, in terms of tokens**: `human:` names the member whose
  Claude seat it runs on (a `claude setup-token` token, valid a year). Who may *use* the
  agent is set separately (`access`). No per-token billing in this version.
- **Models**: per agent in `lab.yaml` (`model:`), else Claude Code's default.
- **Context**: managers auto-compact at 17% of the window (`compact_percent`); others at
  Claude's default.

---

## 11a. Setup and maintenance

**Two Claudes on the secretary host:**

| | Admin assistant | Secretary agent |
|---|---|---|
| What | the admin's own interactive Claude Code session, in a checkout of this repo | the lab's 24/7 service (§4.1) |
| Runs as | the admin's Unix user (SSH and sudo across hosts) | `lab-secretary` |
| Used for | the install, emergencies, uninstalling | everyday administration, in a Slack DM |

**The install** follows `docs/install.md`, guided by the repo's `lab-admin` skill: the
private config repo, `lab init`, the secretary's services, the Lab Approvals and secretary
Slack apps (one at a time), the admin's Claude token, VM power, GPU hosts, the first deploy.

**Only reviewed configuration is deployed.** `lab apply` fetches the config repo and deploys
`lab.yaml` and `framework.lock` (the framework's pinned commit) **as they are on `main`**,
never the working copy, and ships the framework only from a clean checkout at the pinned
commit. Every change: a branch → `lab plan --working-tree` → a pull request (`lab pr-link`) →
a human reviews and merges → deploy. Every host records what it runs
(`/etc/agentic-lab/applied.yaml`). Break glass: `lab apply --unreviewed` (flagged).

**After the install, admins work with the secretary in Slack**, with admin-only tools checked
in code: `propose_lab_change` (a checked branch, pushed; replies with the diff and "here is
the PR, review and merge it"), `offer_deploy` (a **Deploy** button; only an admin's click
deploys), `request_slack_app` (with an app icon Claude draws from the agent's name, role and the lab's name;
`redraw_app_icon` for another), `request_github_token`, `check_slack_app`. The secretary's one
root command is **`lab apply-main`** (a sudoers rule, no arguments): it deploys only `main`,
as its own systemd unit. Even a misled secretary can only deploy what a human merged.

| Command | Does |
|---|---|
| `lab init` | the first `lab.yaml` (and `framework.lock`), with names the admin chose |
| `lab check` | validate `lab.yaml` |
| `lab plan [--working-tree]` | what a deploy would change; missing secrets |
| `lab apply` / `lab apply-main` | deploy `main` (idempotent, over SSH to other hosts) |
| `lab pr-link` | the link to open a pull request for the config branch |
| `lab doctor` | check a host; every failure says how to fix it |
| `lab secret request` | a one-time code or link for a secret |
| `lab slack-manifest`, `lab slack-verify` | a Slack app's manifest; check its tokens |
| `lab nebius connect` | VM power with one admin login |
| `lab logs`, `lab debug` | an agent's log; its session in the console |

**Secrets never pass through the chat** (§6.4). **Uninstalling**: `docs/uninstall.md`, at
the console only.

## 11. Repo layout

```
src/agentic_lab/
  registry/      lab.yaml schema, loader, `lab check`
  policy/        per-agent sandbox settings, grants
  broker/        approval broker + Slack DM channel (Lab Approvals)
  worker/        worker service: MCP server, durable queue, SDK runner, usage
  manager/       manager service: Slack, brain, tools, memory, follow-up, status
  secretary/     state collection, web UI, Home tab, power, chat
  secrets/       intake (one-time codes, web forms)
  apply.py, configrepo.py, lock.py, selfadmin.py   deploying (lab apply / apply-main)
  github.py, slackio.py, slack_manifest.py, eventlog.py, doctor.py, init.py, cli.py, ...
deploy/          systemd units, sandbox setup (AppArmor, bwrap-gpu), user creation
client/          lab-member.py: members hand their Claude token to the lab
docs/            install, uninstall, security notes, architecture (PDF)
examples/        example lab.yaml files
tests/
```

Python ≥ 3.12, managed with `uv`; dependencies: Claude Agent SDK, MCP SDK, slack_sdk,
PyYAML, Starlette/uvicorn.

---

## 12. Milestones

All of M0–M4 are built and were rehearsed end to end on one GPU VM (the whole lab on one
host, each agent its own user: `examples/single-vm/lab.yaml`).

| # | Milestone | Status |
|---|---|---|
| M0 | Skeleton: `lab.yaml` schema, `lab check`, per-agent sandbox settings | done |
| M1 | Worker: MCP server, durable queue, SDK runner, built-in sandbox with GPU, `lab debug` | done |
| M2 | Broker + Lab Approvals in Slack DM: buttons, grants, timeouts | done |
| M3 | Manager: Slack, tasks, follow-up, memory, approvals relay | done |
| M3a | Guided setup: `lab-admin` skill, `lab init/plan/apply/doctor`, intake; config repo; Slack admin | done |
| M4 | Secretary + web UI: state, usage, power and idle stop, notices, Home tab | done |
| M5 | First real lab: install and use it with a real team | next |

---

## 13. Decisions log

| Date | Decision |
|---|---|
| 2026-10-17 | The secretary runs on an always-on host. Other hosts are cloud VMs, as close to real use as possible. |
| 2026-10-17 | Repo is private now and public later, so it stays generic. MIT license. |
| 2026-10-17 | Shared workers: **first come, first served**. No quotas or priorities. |
| 2026-10-17 | **No fixed results contract**: a result is whatever fits (report, commit, link, files). |
| 2026-10-17 | **Agent SDK runtime.** (An `AgentRunner` interface with a tmux fallback was planned; not built, the SDK was enough.) Humans watch agents in the web UI and debug with `lab debug <agent>`. |
| 2026-10-17 | **Every agent is attached to a human member in terms of tokens** (runs on their seat). Access is set separately. |
| 2026-10-17 | **One simple sandbox model for all agents** (§6.1), enforced by Claude Code's built-in sandbox, no command parsing. **Grants** (once / for a while / standing) customise it (§6.2). |
| 2026-10-17 | ~~Guardrail changes initiated by the manager over MCP (`change_guardrails`).~~ Not built: grants come from answering requests, standing grants from `lab.yaml` PRs. |
| 2026-10-17 | ~~All managers share one cloud CPU VM.~~ Superseded 2026-10-18. |
| 2026-10-17 | **Permissions only in Slack DM** in this version. The **web UI is for monitoring only**. |
| 2026-10-17 | **Agents run as system services, each under its own Unix user** (`lab-<agent-id>`). Nobody logs into these users. |
| 2026-10-17 | **Setup is guided by Claude**: the `lab-admin` skill and `docs/install.md`, over declarative, idempotent `lab` commands. Secrets never pass through the chat. |
| 2026-10-17 | **No `lab console`.** Each agent's event log is shown to admins in the web UI; `lab debug <agent>` opens its session in the console. |
| 2026-10-17 | **Approvals go to the human the action is for**, from one Lab Approvals app held by the broker. Managers, like workers, are `dedicated` or `shared`. |
| 2026-10-17 | **The secretary is in Slack** (DMs, threads it's tagged in, a Home tab); lab-wide notices go to an optional `announcements` channel. |
| 2026-10-17 | **Names**: an id (also the Unix user) plus a `display_name` humans see. |
| 2026-10-17 | **Auto-compaction per agent**: managers at 17%; others at Claude's default. |
| 2026-10-17 | **Agents can't read their lab secrets** (§6.4). |
| 2026-10-17 | **No spend limits.** Transparency through the web UI instead. |
| 2026-10-17 | **Agents' logs are for admins only** (`admin: true` + Tailscale identity). |
| 2026-10-17 | **Usage percentages from a probe** of the seat's rate-limit headers, only while a window is open; "Check now" in the web UI. |
| 2026-10-17 | **Admin-only actions, checked in code**: powering off a busy machine (after confirming), asking for someone else's token. **Busy** includes any process on a GPU or any GPU utilization (idle only after 0% in several readings in a row). |
| 2026-10-17 | **Private config repository**: `lab.yaml` + `framework.lock` (the framework pinned by commit). **Only `main` is deployed**; every change is a reviewed PR. |
| 2026-10-17 | **Administering from Slack**: the secretary proposes PRs and deploys `main` on an admin's click, with one sudo command (`lab apply-main`). The console is for the install and emergencies. |
| 2026-10-18 | **GitHub for agents**: one bot account per agent (may be shared), its token in the agent's environment; safety from GitHub (private repos, fine-grained tokens, branch protection: bots never approve or merge). |
| 2026-10-18 | **No experiment tracker in the framework**: project tools are a lab's extensions. |
| 2026-10-18 | **Managers run on any host**: the secretary host, a VM of their own, or a GPU VM next to their workers. |
| 2026-10-18 | **Managers stay reachable**: a work-size rule, a per-turn time limit (`turn_budget_min`, default 5), and a "still working" reply. |
| 2026-10-18 | **A worker's event log goes only to the secretary's monitoring token.** |
| 2026-10-18 | **Anyone in the lab may talk to any manager.** On a dedicated manager, other members are guests: conversation only, no tools, no work, no memory, 2-minute turns. |
| 2026-10-18 | **`~/.lab-keep-awake`**: anyone working on a lab machine directly keeps it from being stopped. |
| 2026-10-19 | **GitHub Apps as agents' identities**, alongside full bot accounts: `<slug>[bot]`, nothing to create but the app; the service mints its hourly tokens. |
| 2026-10-18 | **Approval requests have no timeout by default** (a member may set `timeout_s`); a request the agent stops waiting for is withdrawn. |

## 14. Open questions

None right now.
