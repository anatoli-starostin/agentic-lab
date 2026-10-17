"""`lab`: the command-line entry point.

    lab check [lab.yaml]                         validate the lab registry
    lab policy explain <agent> <command>         why is this Bash command green or gated?
    lab policy explain <agent> --tool T [--path P]
    lab policy show <agent>                      the agent's effective policy
"""
import argparse
import json
import sys
from dataclasses import asdict

from . import policy, registry


def _load_lab(path):
    lab = registry.load(path)
    errors = [i for i in lab.issues if i.level == "error"]
    if errors:
        for i in errors:
            print(i, file=sys.stderr)
        sys.exit(1)
    return lab


def _n(k, word):
    if k == 1:
        return f"{k} {word}"
    return f"{k} {word[:-1]}ies" if word.endswith("y") else f"{k} {word}s"


def cmd_check(args):
    lab = registry.load(args.lab)
    issues = registry.check(lab)
    for i in issues:
        print(i)
    n_err = sum(i.level == "error" for i in issues)
    n_warn = len(issues) - n_err
    a = [x for x in lab.agents.values() if isinstance(x, dict)]
    counts = [_n(len(lab.humans), "human")]
    counts += [_n(sum(x.get("role") == r for x in a), r) for r in policy.ROLES]
    counts += [_n(len(lab.hosts), "host")]
    print(f"{lab.name or args.lab}: {', '.join(counts)} -- "
          f"{_n(n_err, 'error')}, {_n(n_warn, 'warning')}")
    return 1 if n_err else 0


def _agent_policy(lab, agent):
    if agent not in lab.agents:
        sys.exit(f"lab: no agent {agent!r} (have: {', '.join(lab.agents)})")
    return lab.agent_policy(agent)


def cmd_explain(args):
    lab = _load_lab(args.lab)
    p = _agent_policy(lab, args.agent)
    if args.tool == "Bash":
        if not args.command:
            sys.exit("lab: give a command to explain")
        tool_input = {"command": " ".join(args.command)}
    else:
        tool_input = {"file_path": args.path} if args.path else {}
    v = policy.classify(p, args.tool, tool_input)
    print(v)
    return 0


def cmd_show(args):
    lab = _load_lab(args.lab)
    p = _agent_policy(lab, args.agent)
    d = asdict(p)
    d["git_subcommands"] = sorted(d["git_subcommands"])
    print(json.dumps(d, indent=2, default=list))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab", description="Agentic Lab command line")
    ap.add_argument("--lab", default="lab.yaml", help="path to lab.yaml (default: ./lab.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate lab.yaml")
    c.add_argument("path", nargs="?", help="path to lab.yaml (overrides --lab)")
    c.set_defaults(fn=cmd_check)

    pol = sub.add_parser("policy", help="inspect agents' guardrails")
    psub = pol.add_subparsers(dest="pcmd", required=True)
    e = psub.add_parser("explain", help="classify one tool call for an agent")
    e.add_argument("agent")
    e.add_argument("command", nargs="*", help="the Bash command (for --tool Bash)")
    e.add_argument("--tool", default="Bash")
    e.add_argument("--path", help="file path (for Write/Edit)")
    e.set_defaults(fn=cmd_explain)
    s = psub.add_parser("show", help="print an agent's effective policy")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    if getattr(args, "path", None) and args.cmd == "check":
        args.lab = args.path
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
