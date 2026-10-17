# Agentic Lab

A framework for running a scientific lab as a team of **humans and AI agents**:

- **Workers** run experiments on cloud GPU VMs inside a sandbox, without prompts.
- **Managers** are your team members in Slack. They turn requests into worker tasks and
  relay every approval request to a human. They never approve anything themselves.
- A light **secretary** keeps the infrastructure alive: VM power, agent health, token
  expiry, and a status dashboard.

Anything that crosses the sandbox boundary needs a real human's yes, and the code enforces
that, not a prompt.

Status: **design phase**. See [DESIGN.md](DESIGN.md).

License: MIT.
