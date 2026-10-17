# Agentic Lab

A framework for running a scientific lab as a team of **humans and AI agents**:

- **Workers** run experiments on cloud GPU VMs inside a sandbox, without prompts.
- **Managers** are your team members in Slack. They turn requests into worker tasks and
  relay every approval request to a human. They never approve anything themselves.
- A light **secretary** keeps the infrastructure alive: VM power, agent health, token
  expiry, and the lab's web UI.

Anything that crosses the sandbox boundary needs a real human's yes, and the code enforces
that, not a prompt.

Status: **M0** (skeleton, `lab.yaml` registry, per-agent sandbox settings). See [DESIGN.md](DESIGN.md) for the
design and [docs/security-notes.md](docs/security-notes.md) for known gaps.

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
  policy/     the sandbox model (Claude Code settings per agent) and grants
  registry/   lab.yaml: loading and `lab check`
  cli.py      the `lab` command
examples/minimal/   an example lab.yaml with standing grants
tests/
```

License: MIT.
