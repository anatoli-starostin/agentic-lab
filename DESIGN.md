# Agentic Lab — design

A framework that lets a team of **humans and AI agents run a scientific lab together**:
agents run experiments on GPU machines, coordinate through manager agents that speak for
them in Slack, and a light secretary agent keeps the infrastructure alive. Humans stay in
control of anything that crosses a boundary, through a permission channel that **agents
cannot spoof**.

> Status: draft for review (2026-10-17). Nothing here is implemented yet; most pieces exist
> as prototypes in the spiky project and will be extracted and generalised.

This document is deliberately free of hostnames, tokens and account ids. Those belong in a
deployment's own `lab.yaml` and secret files, which never enter this repo.

---

## 1. Goals and non-goals

**Goals**
- Three agent roles (**secretary**, **manager**, **worker**) with clear jobs, budgets and guardrails.
- **Frictionless inside, approval outside.** A worker does nearly all its work in a sandbox
  with no prompts. Anything that crosses the sandbox boundary needs a real human's yes.
- **Humans are never bypassed.** Agents relay approval requests; they never answer them.
  **Code enforces this, not prompts.**
- **One description of the lab.** A single registry (`lab.yaml`) lists the humans, agents,
  hosts, channels and credentials (with expiry dates), and every component reads it.
- **Close to production from day one.** Workers and managers run on cloud VMs. Only the
  secretary runs on an always-on on-prem box.
- **Portable.** No hard-coded hosts or providers. Nebius, Slack, W&B and Tailscale come
  first, behind small interfaces.

**Non-goals (for now)**
- A general multi-tenant SaaS. One lab = one deployment = one tailnet.
- Replacing W&B or git. Experiment tracking stays in W&B, and code and results stay in git.
- Agents that approve each other. There is no agent-to-agent authority over boundary crossings.

---

## 2. Prerequisites

- A **cloud account** with balance (Nebius first) for GPU workers and CPU manager VMs.
- A **W&B** account (or a self-hosted server) for experiment tracking.
- A **Slack workspace** (for teams). Each manager is its own Slack app and bot user.
- **Tailscale.** Every host is on one tailnet. No service listens on a public interface.
- An **always-on box** for the secretary (a small NUC or similar; no GPU needed).
- An **Anthropic API key** (or Claude subscription) per agent host.

---

## 3. Topology

```
                         ┌─────────────────────── Slack ───────────────────────┐
     humans ─────────────┤  channels / threads        DMs (permission channel) │
                         └──────────▲───────────────────────────▲──────────────┘
                                    │                           │
                    ┌───────────────┴───────────┐   ┌───────────┴───────────┐
                    │ MANAGER VM (cloud, CPU)   │   │ SECRETARY (on-prem)   │
                    │  manager agent (LLM)      │   │  scheduler + checks   │
                    │  approval broker (code) ◄─┼─┐ │  dashboard (html)     │
                    └──────┬────────────────────┘ │ │  chat (small model)   │
                  MCP over │ tailnet              │ │  cloud power keys     │
            ┌──────────────┼────────────────┐     │ └───┬───────────┬───────┘
            ▼              ▼                ▼     │     │ power,    │ health
     ┌────────────┐ ┌────────────┐  ┌────────────┐│     │ restart   │ probes
     │ WORKER VM  │ │ WORKER VM  │  │ WORKER VM  ││     ▼           ▼
     │ GPU, cage  │ │ GPU, cage  │  │ GPU, cage  │└── approval requests
     │ worker svc │ │ worker svc │  │ worker svc │    (worker → broker)
     └────────────┘ └────────────┘  └────────────┘
                   all hosts on one tailnet · W&B · git
```

- **Secretary**: always-on, on-prem, holds the cloud power keys. One per lab.
- **Managers**: cloud CPU VMs, possibly preemptible. These can be one shared VM or one small
  VM per manager. The secretary restarts preempted ones.
- **Workers**: cloud GPU VMs, powered on demand. One worker agent per VM.

---

## 4. Roles

### 4.1 Secretary — "keeps the lights on"

Cheap and always on. **Mostly deterministic code. The LLM only handles conversation.**

| Does | Doesn't |
|---|---|
| Power VMs on and off; report what's running and what it costs | Run research or design experiments |
| Check that agent services came back after a VM restart, and restart them if not | Approve anything for anyone |
| Check each credential's **expiry date** in `lab.yaml` and warn ahead of time | Read secret *values* (it only needs expiry dates, plus its own cloud key) |
| Restart preempted manager VMs | |
| Serve the lab **dashboard** (a static HTML page, tailnet only) | |
| Answer simple analytical questions: "did exp042 finish?", "compare A vs B in W&B" | |
| Politely redirect research tasks to a manager | |

- **Scheduler and checks** run as a systemd service and use no LLM tokens. Each check is a
  small plugin (`vm_state`, `agent_health`, `token_expiry`, `cost_burn`, `disk`) that writes
  into one `state.json`.
- **Dashboard**: `state.json` → a static `index.html`, regenerated every minute and served
  over the tailnet. It shows hosts, agents, tasks in flight, pending approvals (count and age
  only), credentials with time to expiry, and cloud spend.
- **Chat**: a small model (Haiku-class) with **read-only** tools over `state.json`, `lab.yaml`
  and W&B, plus the power tools. Powering a GPU VM *on* costs money, so that action goes
  through the requester's permission channel, the same as any other boundary crossing.
- **Who watches the secretary?** Every manager probes the secretary's health endpoint and
  tells its human if the secretary stops answering. The secretary runs under systemd with
  automatic restart.

### 4.2 Worker — "does the science"

Tied to one GPU VM. Uses tokens freely. Humans don't talk to it directly.

- Runs as a **service** that exposes the **worker MCP server** (§5) on its tailnet address.
- Executes tasks inside the **cage** (§6). In-cage work runs without prompts. A boundary
  crossing becomes an **approval request to the approval broker** of the manager that owns
  the task.
- Can be **dedicated** to one human (only that human's manager may submit tasks) or
  **shared** (any manager may). This is set in `lab.yaml`.
- **Debug console**: a human may `ssh` in and run `lab console`. It attaches to the live
  agent session, streams its transcript and lets the human type into it. Approvals for
  console-typed work go to that human's own permission channel, so the console never
  bypasses the gate.
- Reports progress (and W&B links) back through MCP. Results are committed to git per the
  project's conventions.

### 4.3 Manager — "the team member humans talk to"

On a CPU VM. Has its own **Slack identity**. Uses a moderate number of tokens.

- **Converses** in Slack and turns human requests into worker tasks. It picks the worker,
  writes the task, follows progress and reports results.
- **Always has an active permission channel** with its human (Slack DM, console, Telegram
  or email). It can always say which one is active (§7).
- **Relays worker approval requests** to the human and adds context (what the task is, why
  the worker asks). It **cannot answer them** (§6.3).
- **Guardrails**: it may **read** anything in the org and on the internet without asking.
  Every **write** (GitHub, Google Drive, …) goes through its own permission channel. It may
  use a scratchpad dir on its VM freely.
- **Can read and edit workers' guardrails** through MCP. Edits are versioned and announced
  in the thread. **It cannot edit its own**, because its policy file isn't writable by it
  (§6.2).
- Slack design reuses the spiky "Slack façade" (two processes). The Slack-facing process
  cannot stall and cannot leak secrets. The capable process does the work, and its
  approvals go out-of-band to the human.

---

## 5. The worker protocol (MCP over the tailnet)

Each worker runs an MCP server (streamable HTTP) bound to **its tailnet IP only**.
Managers connect as MCP clients. Every request is authenticated with a per-manager bearer
token listed in `lab.yaml`. A dedicated worker rejects managers other than its owner's.

**Tools exposed to managers**

| Tool | Purpose |
|---|---|
| `submit_task(spec) -> task_id` | Queue a task. `spec` = instruction, project, priority, requester (human), optional budget/deadline. |
| `task_status(task_id)` | `queued \| running \| awaiting_approval \| done \| failed \| cancelled`, plus last progress line and W&B links. |
| `task_result(task_id)` | Final report (text and artifact pointers: git commit, W&B run, files). |
| `cancel_task(task_id)` | Cooperative cancel, then kill after a grace period. |
| `list_tasks(filter)` | Queue overview. |
| `pending_approvals()` | Read-only view of what the worker is blocked on (for context only; see §6.3). |
| `get_guardrails()` / `set_guardrails(patch, reason)` | Read and edit this worker's policy overlay (versioned, audited). |
| `health()` | Liveness, GPU state, disk, queue depth, agent session alive?, versions. |

Progress and approvals flow back as **notifications** (MCP server→client), with polling via
`task_status` as a fallback, so a manager that was offline loses nothing. The queue is
durable on disk, so tasks survive restarts of both the worker service and the agent.

Ordering: **one GPU task at a time per worker by default** (GPU contention silently
corrupts benchmarks). `lab.yaml` may raise this per worker.

---

## 6. Guardrails and the trust model

### 6.1 The cage (from spiky's `agent-cage`)

- `sbox` (bubblewrap) makes the whole filesystem read-only except the work dirs, passes the
  GPU through, cuts the network (or opts into tailnet-only), and masks `~/.ssh`.
- `cage_policy` is a classifier that labels every tool call **ungated / green / gated**.
  Green auto-runs, gated becomes an approval request. Parsing is strict about shell
  operators, redirects, command substitution and git flags. **When something over-gates,
  tune the classifier, don't widen the cage.**

### 6.2 Policy as configuration

Today the classifier hard-codes its allowlists. It becomes an engine that reads layered
policy:

```
policies/base.yaml          # shipped with the framework: the safe defaults
policies/role/<role>.yaml   # worker / manager / secretary defaults
<deploy>/agents/<name>.yaml # per-agent overlay (extra green commands, extra writable dirs,
                            #  network mode, helpers green-listed by absolute path)
```

- A policy file and the engine that enforces it **live outside anything the governed agent
  can write.** That is a separate Unix user, or a dir the cage mounts read-only. An agent
  cannot widen its own guardrails without a human, by construction.
- A manager edits a worker's overlay through `set_guardrails`. The worker service (not the
  worker agent) applies it, keeps the history and announces it.
- `lab policy explain <agent> '<command>'` prints the verdict and the rule that produced it,
  for debugging and for the dashboard.

### 6.3 Approvals: the broker, not the manager, decides who answered

The rule "the manager never grants permissions itself" must hold even if the manager LLM is
confused or prompt-injected. So **approval is a code path that bypasses the LLM**:

```
worker gate ──req──► APPROVAL BROKER (manager VM, separate Unix user, no LLM)
                          │  ├─► notifies manager agent: "approval pending" (read-only, for context)
                          │  └─► human's permission channel (Slack DM buttons / console / tg / email)
                          ◄── human decision (verified: Slack-signed interaction, console tty, …)
worker gate ◄─signed decision── broker   (HMAC with a per-worker key the manager agent can't read)
```

- The worker gate accepts only decisions **signed by the broker**. The broker signs only
  after a **verified human interaction** on a registered channel.
- The manager agent can **see** pending requests and **add context** to the message shown to
  the human ("this is the pip install from task 17, needed for the eval"). It has no tool
  that produces a decision.
- **Timeouts deny.** An unanswered request auto-denies after `N` seconds (configurable per
  human and channel). The denial reason tells the worker it was a timeout, not a "no", and
  asks it to find an in-cage way first. (This is spiky's lesson: cheap paths in-cage keep
  real approvals rare and meaningful.)
- **"Always allow" belongs to the human, not the agent.** An "always" click adds a scoped
  rule to that worker's policy overlay, through the same versioned path as `set_guardrails`.
- **Typed reply = redirect.** A human typing an answer instead of clicking means "no, do it
  this way". The note goes back to the worker as an instruction.

The manager's *own* boundary crossings (GitHub and Drive writes) go through **the same
broker** and the same channel. The manager agent is just another gated client.

### 6.4 Secrets

- Secrets live in per-host files (`chmod 600`, owned by the service user) and never in the
  repo, `lab.yaml`, Slack or agent memory.
- `lab.yaml` lists each credential's **name, owner host, scope and expiry date**, never its
  value. The secretary needs only that to warn about expiry.
- The cage masks secret dirs from caged processes. A Slack-facing process holds only its own
  Slack tokens and has no filesystem reach beyond its own workspace.

---

## 7. Permission channels

One interface, several transports:

```python
class PermissionChannel:
    name: str                                   # "slack_dm" | "console" | "telegram" | "email"
    async def ask(self, req: ApprovalRequest) -> Decision   # approve / deny / always / redirect(note) / timeout
    def describe(self) -> str                   # "Slack DM with @anatoly, auto-deny after 120s"
```

**Per-human channel policy** in `lab.yaml`:

```yaml
humans:
  anatoly:
    slack_user: U0123ABC
    channels:
      - {type: slack_dm, timeout_s: 120, on_timeout: deny}
      - {type: console,  when: "attached"}     # console wins while the human is at it
      - {type: telegram, timeout_s: 300, on_timeout: deny, quiet_hours: "23:00-08:00"}
```

- **The active channel follows the request's origin**: Slack-delegated work asks in Slack,
  console-typed work asks at the console. Spiky calls this switch "the cord". Restarts reset
  to the safest default.
- Every manager can always say which channel is active and why ("your approvals go to your
  Slack DM; the console is detached").
- Possible later options: escalation to a second channel if the first times out, batching
  several requests into one message, and per-category defaults ("always deny pushes to
  `main`").

---

## 8. The lab registry: `lab.yaml`

The one source of truth that every component reads. An example deployment:

```yaml
lab: deepspike
tailnet: example.ts.net
providers:
  nebius: {project: ..., key_host: secretary}       # only the secretary holds the key
humans:
  anatoly: {...}                                    # §7
agents:
  secretary:
    role: secretary
    host: nuc-01
    model: claude-haiku-5-5
  mgr-anatoly:
    role: manager
    human: anatoly
    host: mgr-vm-01
    slack_app: A0XXXX
    model: claude-sonnet-5-5
  worker-h100-a:
    role: worker
    host: gpu-h100-a
    access: shared            # or: {dedicated: anatoly}
    max_parallel_gpu_tasks: 1
    policy_overlay: agents/worker-h100-a.yaml
hosts:
  nuc-01:      {kind: onprem}
  mgr-vm-01:   {kind: cloud, provider: nebius, instance: ..., preemptible: true,  restart_by: secretary}
  gpu-h100-a:  {kind: cloud, provider: nebius, instance: ..., power: on_demand, idle_stop_min: 30}
credentials:
  - {name: slack/mgr-anatoly, host: mgr-vm-01, expires: null}
  - {name: github/bot-pat,     host: gpu-h100-a, expires: 2026-12-11, scope: "repo"}
  - {name: nebius/api-key,     host: nuc-01,     expires: 2027-03-01}
```

A small CLI validates it (`lab check`) and renders it into the dashboard.

---

## 9. Agent runtime

**Recommendation: every agent runs on the Claude Agent SDK as a systemd service.** A
terminal session that pairs itself with helper processes is fragile; spiky's approach has
hit a 30-minute monitor limit and needs re-arming.

- The SDK gives an in-process permission callback. That callback *is* the gate
  (`cage_policy` → broker), so "never prompts the wrong place" is enforceable.
- Sessions persist on disk and resume after restarts.
- `lab console` (§4.2) gives humans an interactive view of a headless agent, replacing
  "attach to a tmux pane".
- Each agent gets its own Unix user, working dir and memory dir. The policy and the broker
  run as different users from the agent they govern.

Model per role is set in `lab.yaml`. Defaults: Haiku-class for the secretary's chat,
Sonnet-class for managers, the strongest available model for workers.

---

## 10. Repo layout (proposed)

```
agentic_lab/
  registry/      lab.yaml schema, loader, `lab check`
  policy/        classifier engine (from cage_policy) + base/role policies + tests
  cage/          sbox (bubblewrap) + host setup notes
  broker/        approval broker + permission channels (slack_dm, console, telegram, email)
  worker/        worker service: MCP server, durable queue, agent runner, `lab console`
  manager/       manager service: Slack face (from slack-facade) + MCP client + agent runner
  secretary/     scheduler, checks, providers (nebius), dashboard renderer, chat
  providers/     cloud provider interface + nebius implementation
deploy/          systemd units, bootstrap scripts (`lab install <role>`), example lab.yaml
docs/            this design, runbooks, threat model
tests/
```

Python ≥ 3.12, managed with `uv`. Only stdlib plus a few dependencies (MCP SDK, Agent SDK,
slack_sdk, PyYAML).

---

## 11. Milestones

Each milestone ends with a demo that works on real infrastructure.

| # | Milestone | Done when |
|---|---|---|
| M0 | **Skeleton**: repo, `lab.yaml` schema + `lab check`, policy engine ported with its full test battery | `pytest` green; spiky's `cage_policy` test cases pass against the new engine |
| M1 | **Worker**: MCP server, durable queue, SDK agent runner, cage, `lab console` | A manager played by hand over MCP submits a task to a cloud GPU VM, watches progress and gets the result |
| M2 | **Broker + channels**: signed decisions, Slack DM + console channels, timeout policy | A worker's `pip install` reaches the human's DM. Approve and deny both work, the timeout denies, and **the manager agent provably cannot approve** (test) |
| M3 | **Manager**: Slack face + MCP client + its own guardrails | "@manager run exp X on a GPU worker" in Slack → task → approval in DM → result in thread |
| M4 | **Secretary**: checks, cloud power, dashboard, chat | Dashboard live. A GPU VM idle 30 min is stopped. A preempted manager is restarted. A token expiring within 14 days is reported |
| M5 | **Spiky pilot**: real spiky experiments run through the lab for a week | Spiky's existing ad-hoc tooling retired in favour of the lab |
| M6 | **DeepSpike rollout**: separate deployment, its own `lab.yaml`, Slack workspace and tailnet | Team members use their own managers |

Telegram and email channels, multiple workers per manager, and preemptible-VM tuning come
after M3–M4 as needed.

---

## 12. Open questions

1. **Worker runtime**: is Agent SDK + `lab console` acceptable instead of an attachable
   Claude Code terminal? (Recommended: yes.)
2. **Who can submit to a shared worker** when two managers compete: FIFO, per-human quota or
   priority?
3. **Results contract**: must every task end in a git commit and/or a W&B run, or is a text
   report enough for small tasks?
4. **Cost guardrails**: should GPU power-on above a daily spend threshold need an extra approval?
5. **Manager VM layout**: one shared VM with one Unix user per manager, or one small VM per
   manager? (Start shared; it's cheaper and the secretary restarts it.)
