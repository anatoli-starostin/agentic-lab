"""`lab`: the command-line entry point.

    lab check [lab.yaml]        validate the lab registry
    lab settings <agent>        the agent's generated Claude Code settings (sandbox + grants)
    lab worker serve <agent>    run a worker's service (as its own Unix user, from systemd)
"""
import argparse
import json
import sys

from . import registry
from .registry.lab import ROLES


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
    counts += [_n(sum(x.get("role") == r for x in a), r) for r in ROLES]
    counts += [_n(len(lab.hosts), "host")]
    print(f"{lab.name or args.lab}: {', '.join(counts)} -- "
          f"{_n(n_err, 'error')}, {_n(n_warn, 'warning')}")
    return 1 if n_err else 0


def cmd_settings(args):
    lab = _load_lab(args.lab)
    if args.agent not in lab.agents:
        sys.exit(f"lab: no agent {args.agent!r} (have: {', '.join(lab.agents)})")
    print(json.dumps(lab.agent_settings(args.agent), indent=2))
    return 0


def cmd_worker_serve(args):
    import asyncio
    import logging

    from .worker.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    names = [n for n in (args.name or "").split(",") if n]
    asyncio.run(serve(args.lab, args.agent, args.host, args.port, model=args.model,
                      names=names))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab", description="Agentic Lab command line")
    ap.add_argument("--lab", default="lab.yaml", help="path to lab.yaml (default: ./lab.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate lab.yaml")
    c.add_argument("path", nargs="?", help="path to lab.yaml (overrides --lab)")
    c.set_defaults(fn=cmd_check)

    st = sub.add_parser("settings", help="print an agent's generated Claude Code settings")
    st.add_argument("agent")
    st.set_defaults(fn=cmd_settings)

    wk = sub.add_parser("worker", help="the worker service")
    wsub = wk.add_subparsers(dest="wcmd", required=True)
    ws = wsub.add_parser("serve", help="run a worker's service")
    ws.add_argument("agent")
    ws.add_argument("--host", default="127.0.0.1", help="address to serve MCP on "
                    "(the host's tailnet IP in production)")
    ws.add_argument("--port", type=int, default=8770)
    ws.add_argument("--name", help="comma-separated host names managers use to reach this "
                    "worker, e.g. its tailnet name (besides --host)")
    ws.add_argument("--model", help="override the agent's model from lab.yaml")
    ws.set_defaults(fn=cmd_worker_serve)

    args = ap.parse_args(argv)
    if getattr(args, "path", None) and args.cmd == "check":
        args.lab = args.path
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
