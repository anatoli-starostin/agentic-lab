"""`lab`: the command-line entry point.

    lab check [lab.yaml]        validate the lab registry
    lab settings <agent>        the agent's generated Claude Code settings (sandbox + grants)
    lab worker serve <agent>    run a worker's service (as its own Unix user, from systemd)
    lab secret request <human> <name> --url URL
                                ask a human for a secret: prints the message to send them
    lab intake serve            the secretary's endpoint where members' clients send secrets
    lab broker serve            the approval broker (on the secretary host, as lab-broker)
    lab manager serve <agent>   run a manager's service (Slack + its agent)
    lab manager ask <agent> --as <member> "message"
                                one message to a manager, answered on the console
    lab doctor [--host H]       (sudo) check this host against lab.yaml, with fixes
    lab slack-manifest <agent>|approvals
                                the Slack app manifest for an agent (paste into api.slack.com)
    lab debug <agent>           (sudo) stop the agent's service and open its session in the
                                classic Claude Code console; restarts the service on exit
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


STATE = "~/.local/state/agentic-lab"


def cmd_secret_request(args):
    import os

    from .secrets.intake import Intake, form_url, message
    lab = _load_lab(args.lab)
    owner = args.human
    if owner.startswith("service:"):
        if args.name != "slack-app":
            sys.exit("lab: services hand over slack-app secrets only")
    elif owner not in lab.humans:
        sys.exit(f"lab: no human {owner!r} in {args.lab}")
    intake = Intake(os.path.expanduser(args.state))
    code = intake.request(owner, args.name)
    if args.form or owner.startswith("service:"):
        print(form_url(code, args.url))
    else:
        name = (lab.humans[owner] or {}).get("name", owner)
        print(message(name, code, args.url, args.name))
    return 0


def cmd_intake_serve(args):
    import os

    import uvicorn

    from .secrets.intake import Intake
    from .secrets.server import app
    intake = Intake(os.path.expanduser(args.state))
    uvicorn.run(app(intake, args.client), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_broker_serve(args):
    import asyncio
    import logging

    from .broker.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(serve(args.lab, args.host, args.port))
    return 0


def cmd_manager_serve(args):
    import asyncio
    import logging

    from .manager.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(serve(args.lab, args.agent))
    return 0


def cmd_slack_manifest(args):
    from . import slack_manifest as sm
    lab = _load_lab(args.lab)
    if args.agent == "approvals":
        print(sm.render(sm.approvals(lab, args.name or "Lab Approvals")))
    elif args.agent in lab.agents:
        try:
            print(sm.render(sm.for_agent(lab, args.agent)))
        except ValueError as e:
            sys.exit(f"lab: {e}")
    else:
        sys.exit(f"lab: no agent {args.agent!r}")
    return 0


def cmd_manager_ask(args):
    import asyncio
    import logging

    from .manager.service import ask
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(ask(args.lab, args.agent, args.as_human, " ".join(args.message),
                    wait_s=args.wait))
    return 0


def cmd_doctor(args):
    from .doctor import report, run
    text, failed = report(run(args.lab, host=args.host))
    print(text)
    return 1 if failed else 0


def cmd_debug(args):
    from .debug import debug
    debug(args.lab, args.agent, session=args.session, new=args.new, ask=args.print)
    return 0


def cmd_debug_session(args):
    from .debug import debug_session
    debug_session(args.lab, args.agent, session=args.session, new=args.new, ask=args.print)
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

    sc = sub.add_parser("secret", help="secrets from humans")
    ssub = sc.add_subparsers(dest="scmd", required=True)
    sr = ssub.add_parser("request", help="ask a human for a secret (one-time code + message)")
    sr.add_argument("human", help="the owner: a human id, or service:<name> (e.g. service:broker)")
    sr.add_argument("name", choices=["claude-token", "slack-app"])
    sr.add_argument("--form", action="store_true",
                    help="print a one-time web-form link instead of the client command")
    sr.add_argument("--url", required=True, help="the intake's address as members reach it, "
                    "e.g. http://secretary.example.ts.net:8771")
    sr.add_argument("--state", default=STATE)
    sr.set_defaults(fn=cmd_secret_request)

    it = sub.add_parser("intake", help="the secret intake endpoint")
    isub = it.add_subparsers(dest="icmd", required=True)
    iserve = isub.add_parser("serve", help="serve the intake (as the secretary's user)")
    iserve.add_argument("--host", default="127.0.0.1", help="the tailnet IP in production")
    iserve.add_argument("--port", type=int, default=8771)
    iserve.add_argument("--state", default=STATE)
    iserve.add_argument("--client", default="/opt/agentic-lab/client/lab-member.py",
                        help="the member client file to serve")
    iserve.set_defaults(fn=cmd_intake_serve)

    bk = sub.add_parser("broker", help="the approval broker")
    bsub = bk.add_subparsers(dest="bcmd", required=True)
    bserve = bsub.add_parser("serve", help="run the broker")
    bserve.add_argument("--host", default="127.0.0.1", help="the tailnet IP in production")
    bserve.add_argument("--port", type=int, default=8772)
    bserve.set_defaults(fn=cmd_broker_serve)

    mg = sub.add_parser("manager", help="the manager service")
    msub = mg.add_subparsers(dest="mcmd", required=True)
    mserve = msub.add_parser("serve", help="run a manager's service")
    mserve.add_argument("agent")
    mserve.set_defaults(fn=cmd_manager_serve)
    mask = msub.add_parser("ask", help="one message to a manager, answered on the console")
    mask.add_argument("agent")
    mask.add_argument("message", nargs="+")
    mask.add_argument("--as", dest="as_human", required=True, help="the member asking")
    mask.add_argument("--wait", type=int, default=900,
                      help="seconds to follow the tasks it submits (default 900)")
    mask.set_defaults(fn=cmd_manager_ask)

    dr = sub.add_parser("doctor", help="check this host against lab.yaml (run with sudo)")
    dr.add_argument("--host", help="which lab.yaml host this is (default: detect)")
    dr.set_defaults(fn=cmd_doctor)

    sm = sub.add_parser("slack-manifest", help="print a Slack app manifest")
    sm.add_argument("agent", help="a manager or the secretary, or 'approvals'")
    sm.add_argument("--name", help="the approvals app's name (default: Lab Approvals)")
    sm.set_defaults(fn=cmd_slack_manifest)

    for name, fn, hlp in (
            ("debug", cmd_debug, "talk to an agent in the classic Claude Code console (sudo)"),
            ("debug-session", cmd_debug_session, argparse.SUPPRESS)):
        d = sub.add_parser(name, help=hlp)
        d.add_argument("agent")
        d.add_argument("--session", help="open this session instead of the latest task's")
        d.add_argument("--new", action="store_true", help="start a fresh session")
        d.add_argument("--print", metavar="QUESTION",
                       help="ask the session one question, non-interactively")
        d.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    if getattr(args, "path", None) and args.cmd == "check":
        args.lab = args.path
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
