# Agentic Lab

A framework for running a scientific lab as a team of **humans and AI agents**:

- **Workers** run experiments on cloud GPU VMs inside a sandbox, without prompts.
- **Managers** are your team members in Slack. They turn requests into worker tasks and
  relay every approval request to a human. They never approve anything themselves.
- A light **secretary** keeps the infrastructure alive: VM power, agent health, token
  expiry, and the lab's web UI.

Anything that crosses the sandbox boundary needs a real human's yes, and the code enforces
that, not a prompt.

Status: **v0, working end to end** on a GPU VM with a real Slack workspace: workers
running experiments in Claude Code's built-in sandbox, approvals and grants in Slack DMs,
managers and a secretary in Slack, VM power, a web UI, and a guided setup. See
[DESIGN.md](DESIGN.md), [docs/install.md](docs/install.md) and
[docs/security-notes.md](docs/security-notes.md).

## Install

Three steps by hand, then Claude guides the rest (`docs/install.md`):

1. Allocate a small always-on VM for the lab's secretary (Ubuntu 24.04+).
2. Install Claude Code on it and log in.
3. Tell Claude: *"Set up an Agentic Lab on this machine:
   https://github.com/anatoli-starostin/agentic-lab"*. It reads `BOOTSTRAP.md` and takes over.

## Development quick start

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                                         # registry, sandbox and grant tests
.venv/bin/lab check examples/minimal/lab.yaml            # validate a lab registry
.venv/bin/lab --lab examples/minimal/lab.yaml settings worker-a   # its Claude Code settings
```

## Layout

```
src/agentic_lab/
  registry/   lab.yaml: loading, `lab check`
  policy/     the sandbox model (Claude Code settings per agent) and grants
  worker/     the worker service: task queue, MCP API, Agent SDK runner, usage
  broker/     approvals: requests, decisions, grants, the Lab Approvals Slack app
  manager/    the manager: Slack conversations, delegation to workers, memory, follow-ups
  secretary/  lab state, web UI, VM power, its Slack chat
  secrets/    the intake: one-time codes and forms (secrets never pass through chat)
  slackio.py  what an agent sees in Slack, and how it answers
  apply.py    `lab plan` / `lab apply`;  doctor.py, init.py, debug.py, nebius_connect.py
client/       lab-member.py: the one file a member ever runs
deploy/       host setup (sandbox, GPU, AppArmor), systemd units
examples/     lab.yaml examples (minimal, single VM)
```

License: MIT.
