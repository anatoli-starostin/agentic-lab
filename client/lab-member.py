#!/usr/bin/env python3
"""lab-member: the small client a human member of an Agentic Lab runs on their own machine.

When the lab needs something only you can give (today: your Claude seat token for your
agents), the secretary sends you a one-time code and a command line. Run it here:

    python3 lab-member.py claude-token --code K7QM-XP4D --to http://<secretary>:8771

It gets the token: it runs `claude setup-token` for you and picks the token up from its
output. If the token was shown elsewhere (say, in your browser) you paste it, hidden and sends it straight to the lab's secretary over the tailnet. The token
never goes through Slack or a chat. The code works once, for one secret, for a limited time.

Requires only Python 3.8+ (standard library) and, for `claude setup-token`, Claude Code.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

VERSION = 1


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
_TOKEN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def run_capturing(argv):
    """Run an interactive command in a pseudo-terminal (it looks and behaves as usual) and
    return (exit code, everything it printed). Unix only."""
    import pty
    out = bytearray()

    def read(fd):
        data = os.read(fd, 4096)
        out.extend(data)
        return data

    status = pty.spawn(argv, read)
    code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
    return code, out.decode(errors="replace")


def find_token(text):
    """The last sk-ant- token in terminal output. The terminal may have wrapped it over
    several lines, so a line break (and the indentation after it) doesn't end it."""
    text = _ANSI.sub("", text).replace("\r", "")
    start = text.rfind("sk-ant-")
    if start < 0:
        return None
    token, i = [], start
    while i < len(text):
        c = text[i]
        if c in _TOKEN_CHARS:
            token.append(c)
            i += 1
        elif c == "\n":
            j = i + 1
            while j < len(text) and text[j] == " ":
                j += 1
            if j < len(text) and text[j] in _TOKEN_CHARS and text[j] != "\n":
                i = j          # the token continues on the next line
            else:
                break
        else:
            break
    return "".join(token)


def looks_like_token(t):
    return bool(t) and t.startswith("sk-ant-") and len(t) >= 40


def get_claude_token(paste):
    if not paste and shutil.which("claude"):
        print("Running `claude setup-token`: approve in your browser, as usual.")
        print("(Your agents use this token to run on your Claude seat. It is valid for a year.)\n")
        try:
            code, out = run_capturing(["claude", "setup-token"])
        except (ImportError, OSError):          # no pseudo-terminals here (e.g. Windows)
            code, out = subprocess.run(["claude", "setup-token"]).returncode, ""
        if code != 0:
            sys.exit("`claude setup-token` failed; fix that, or re-run with --paste")
        token = find_token(out)
        if looks_like_token(token):
            print(f"\nFound the token: {token[:13]}…{token[-4:]} ({len(token)} characters).")
            if input("Send it to the lab? [Y/n] ").strip().lower() in ("", "y", "yes"):
                return token
        print("\nCopy the token from wherever it was shown: the output above, or your browser.")
    elif not paste:
        print("Claude Code isn't installed here. Get a token with `claude setup-token` on any")
        print("machine where it is, then paste it below.\n")
    token = getpass.getpass("Paste the token (input hidden; from the terminal or the browser), "
                            "then Enter: ").strip()
    if not looks_like_token(token):
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
                    help="paste a token you already have (e.g. shown in your browser) instead "
                         "of running claude setup-token")
    args = ap.parse_args(argv)

    if args.cmd == "claude-token":
        token = get_claude_token(args.paste)
        reply = send(args.to, args.code.strip().upper(), "claude-token", token)
        print(f"Done: the lab stored your Claude token ({reply.get('stored', 'ok')}).")
        print("You can close this window.")


if __name__ == "__main__":
    main()
