#!/usr/bin/env python3
"""lab-member: the small client a human member of an Agentic Lab runs on their own machine.

When the lab needs something only you can give (today: your Claude seat token for your
agents), the secretary sends you a one-time code and a command line. Run it here:

    python3 lab-member.py claude-token --code K7QM-XP4D --to http://<secretary>:8771

It gets the token (running `claude setup-token` for you, or asking you to paste one, with
the input hidden) and sends it straight to the lab's secretary over the tailnet. The token
never goes through Slack or a chat. The code works once, for one secret, for a limited time.

Requires only Python 3.8+ (standard library) and, for `claude setup-token`, Claude Code.
"""
import argparse
import getpass
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

VERSION = 1


def get_claude_token(paste):
    if not paste and shutil.which("claude"):
        print("Running `claude setup-token`: approve in your browser; it then prints a token.")
        print("(Your agents use this token to run on your Claude seat. It is valid for a year.)\n")
        r = subprocess.run(["claude", "setup-token"])
        if r.returncode != 0:
            sys.exit("`claude setup-token` failed; fix that, or re-run with --paste")
        print()
    elif not paste:
        print("Claude Code isn't installed here. Get a token with `claude setup-token` on any")
        print("machine where it is, then paste it below.\n")
    token = getpass.getpass("Paste the token (input hidden), then Enter: ").strip()
    if not token.startswith("sk-ant-") or len(token) < 40:
        sys.exit("that doesn't look like a Claude token (it starts with sk-ant-); nothing sent")
    return token


def send(to, code, name, value):
    body = json.dumps({"version": VERSION, "code": code, "name": name, "value": value}).encode()
    req = urllib.request.Request(to.rstrip("/") + "/v1/secret", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        sys.exit(f"the secretary refused it ({e.code}): {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"can't reach the secretary at {to}: {e.reason}. Are you on the lab's tailnet?")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab-member", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ct = sub.add_parser("claude-token", help="give the lab your Claude seat token")
    ct.add_argument("--code", required=True, help="the one-time code from the secretary")
    ct.add_argument("--to", required=True, help="the secretary's address (from its message)")
    ct.add_argument("--paste", action="store_true",
                    help="paste a token you already have instead of running claude setup-token")
    args = ap.parse_args(argv)

    if args.cmd == "claude-token":
        token = get_claude_token(args.paste)
        reply = send(args.to, args.code.strip().upper(), "claude-token", token)
        print(f"Done: the lab stored your Claude token ({reply.get('stored', 'ok')}).")
        print("You can close this window.")


if __name__ == "__main__":
    main()
