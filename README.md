# Agentic Lab

A framework for running a scientific lab as a team of **humans and AI agents**:

- **Workers** run experiments on cloud GPU VMs inside a sandbox, without prompts.
- **Managers** are your team members in Slack. They turn requests into worker tasks and
  relay every approval request to a human. They never approve anything themselves.
- A light **secretary** keeps the infrastructure alive: VM power, agent health, token
  expiry, and the lab's web UI.

Anything that crosses the sandbox boundary needs a real human's yes, and the code enforces
that, not a prompt.

Status: **M0** (skeleton, registry, policy engine). See [DESIGN.md](DESIGN.md) for the
design and [docs/security-notes.md](docs/security-notes.md) for known gaps.

## Quick start

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                                     # the policy and registry tests
.venv/bin/lab check examples/minimal/lab.yaml        # validate a lab registry
.venv/bin/lab --lab examples/minimal/lab.yaml policy explain worker-a "git push origin main"
.venv/bin/lab --lab examples/minimal/lab.yaml policy show mgr-alice
```

## Layout

```
src/agentic_lab/
  policy/     guardrails as config: base.yaml, roles/*.yaml, the classifier engine
  registry/   lab.yaml: loading and `lab check`
  cage/       sbox, the bubblewrap sandbox
  cli.py      the `lab` command
examples/minimal/   an example lab.yaml with a per-agent policy overlay
tests/
```

License: MIT.
