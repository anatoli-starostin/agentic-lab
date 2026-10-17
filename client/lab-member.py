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


# one escape sequence: CSI (with its parameters and final byte), OSC ended by BEL or ST
# (hyperlinks), charset selection, or a two-byte one (`ESC 7`, `ESC 8`)
_ESC = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                  r"|\x1b[()*+#].|\x1b(.)")
_TOKEN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def run_capturing(argv):
    """Run an interactive command in a pseudo-terminal (it looks and behaves as usual) and
    return (exit code, everything it printed). Unix only.

    Not pty.spawn: it gives the child an 80-column terminal whatever the real one is (so a
    long token wraps), and on macOS it keeps waiting for a key press after the child exits."""
    import fcntl
    import pty
    import select
    import termios
    import tty
    out = bytearray()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    stdin = sys.stdin.fileno()
    interactive = os.isatty(stdin)
    saved = None
    if interactive:
        try:
            size = fcntl.ioctl(stdin, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass
        saved = termios.tcgetattr(stdin)
        tty.setraw(stdin)
    status = None
    try:
        while status is None:
            ready, _, _ = select.select([fd, stdin] if interactive else [fd], [], [], 0.2)
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:             # Linux: the child closed the terminal
                    data = b""
                if data:
                    out.extend(data)
                    os.write(sys.stdout.fileno(), data)
            if stdin in ready:
                data = os.read(stdin, 4096)
                if data:
                    os.write(fd, data)
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
        while select.select([fd], [], [], 0.1)[0]:      # what it printed just before exiting
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            out.extend(data)
            os.write(sys.stdout.fileno(), data)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin, termios.TCSAFLUSH, saved)
        os.close(fd)
    code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
    return code, out.decode(errors="replace")


def screen(text):
    """What `text` (a terminal UI's output) leaves on the screen, as lines. Terminal UIs
    draw with cursor moves (Claude Code writes `ESC[7G` where a space goes), so stripping
    the escape sequences isn't enough: this replays the moves that matter."""
    rows, r, c, saved = [[]], 0, 0, (0, 0)

    def put(ch):
        row = rows[r]
        row.extend(" " * (c + 1 - len(row)))
        row[c] = ch

    i = 0
    while i < len(text):
        m = _ESC.match(text, i) if text[i] == "\x1b" else None
        if m:
            i = m.end()
            params, final, two = m.group(1), m.group(2), m.group(3)
            if final:
                if params.startswith(("?", ">", "<", "=")):
                    continue                            # modes and queries: no drawing
                nums = [int(x) if x.isdigit() else 0 for x in params.split(";")] if params else []
                n = max(nums[0], 1) if nums else 1
                if final == "G":
                    c = n - 1
                elif final == "C":
                    c += n
                elif final == "D":
                    c = max(c - n, 0)
                elif final in "AF":
                    r, c = max(r - n, 0), (0 if final == "F" else c)
                elif final in "BE":
                    r, c = r + n, (0 if final == "E" else c)
                elif final in "Hf":
                    r = (nums[0] if nums else 1) - 1
                    c = (nums[1] if len(nums) > 1 and nums[1] else 1) - 1
                elif final == "K":
                    mode = nums[0] if nums else 0
                    if mode == 0:
                        del rows[r][c:]
                    else:
                        rows[r] = [] if mode == 2 else [" "] * min(c + 1, len(rows[r]))
                elif final == "J" and nums and nums[0] in (2, 3):
                    rows, r, c = [[]], 0, 0
                elif final == "s":
                    saved = (r, c)
                elif final == "u":
                    r, c = saved
            elif two == "7":
                saved = (r, c)
            elif two == "8":
                r, c = saved
        else:
            ch = text[i]
            i += 1
            if ch == "\r":
                c = 0
            elif ch == "\n":
                r += 1
            elif ch == "\b":
                c = max(c - 1, 0)
            elif ch == "\t":
                c = (c // 8 + 1) * 8
            elif ch >= " ":
                put(ch)
                c += 1
        while len(rows) <= r:
            rows.append([])
    return ["".join(row).rstrip() for row in rows]


def find_token(text):
    """The last sk-ant- token on the screen `text` leaves. A long token may be wrapped over
    several lines: a following line counts as more of it only if it is nothing but token
    characters (so "Store this token securely." below it doesn't)."""
    lines = screen(text)
    for n in range(len(lines) - 1, -1, -1):
        start = lines[n].rfind("sk-ant-")
        if start >= 0:
            break
    else:
        return None
    line = lines[n][start:]
    end = 0
    while end < len(line) and line[end] in _TOKEN_CHARS:
        end += 1
    token = line[:end]
    if line[end:].strip():                  # more text right after it: not wrapped
        return token
    for nxt in lines[n + 1:]:
        part = nxt.strip()
        if not part or not set(part) <= _TOKEN_CHARS:
            break
        token += part
    return token


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
    return paste_token()


class Refused(Exception):
    pass


def paste_token():
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
        raise Refused(f"the secretary refused it ({e.code}): {detail}") from None
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
        code = args.code.strip().upper()
        token = get_claude_token(args.paste)
        try:
            reply = send(args.to, code, "claude-token", token)
        except Refused as e:
            if args.paste or "rejected this token" not in str(e):
                sys.exit(str(e))
            print(f"\n{e}")        # e.g. a token cut short; the code is still usable
            print("Copy the whole token from the output above (every line of it).")
            try:
                reply = send(args.to, code, "claude-token", paste_token())
            except Refused as e:
                sys.exit(str(e))
        print(f"Done: the lab stored your Claude token ({reply.get('stored', 'ok')}).")
        print("You can close this window.")


if __name__ == "__main__":
    main()
