"""`lab slack verify <owner>`: right after a Slack app's tokens arrive, check them.

Run on the secretary host with sudo (it reads the intake's store). Asks Slack who the bot
is (`auth.test`) and which app it belongs to (`bots.info`), checks the app-level token can
open a Socket Mode connection (`apps.connections.open`), and prints the app's name,
workspace and App ID. With `--write`, records the App ID in lab.yaml (`slack_app`) for a
manager, so nobody has to look it up. Tokens are never printed.
"""
import json
import urllib.parse
import urllib.request

from .apply import intake_secret


class VerifyError(RuntimeError):
    pass


def _call(method, token, params=None):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def verify(owner, store_get=intake_secret, call=_call):
    """{name, team, app_id, bot_user, socket_ok} for the Slack app `owner` handed over."""
    env = store_get(owner, "slack-app")
    if env is None:
        raise VerifyError(f"no Slack app tokens for {owner} yet (the one-time link not used?)")
    vals = dict(line.split("=", 1) for line in env.strip().splitlines() if "=" in line)
    bot, app = vals.get("SLACK_BOT_TOKEN", ""), vals.get("SLACK_APP_TOKEN", "")
    who = call("auth.test", bot)
    if not who.get("ok"):
        raise VerifyError(f"the bot token doesn't work: {who.get('error')}")
    info = call("bots.info", bot, {"bot": who.get("bot_id", "")})
    b = info.get("bot") or {}
    sock = call("apps.connections.open", app)
    return {"name": b.get("name") or who.get("user"), "team": who.get("team"),
            "app_id": b.get("app_id"), "bot_user": who.get("user_id"),
            "socket_ok": bool(sock.get("ok")), "socket_error": sock.get("error")}


def write_app_id(lab_path, agent, app_id):
    """Record a manager's App ID in lab.yaml, keeping the file as written otherwise."""
    with open(lab_path) as f:
        lines = f.read().split("\n")
    out, i, done = [], 0, False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].rstrip() == f"  {agent}:":
            j = i + 1
            while j < len(lines) and lines[j].startswith("    "):
                if not lines[j].lstrip().startswith("slack_app:"):
                    out.append(lines[j])
                j += 1
            out.append(f"    slack_app: {app_id}")
            i, done = j, True
            continue
        i += 1
    if not done:
        raise VerifyError(f"no agent {agent!r} in {lab_path}")
    with open(lab_path, "w") as f:
        f.write("\n".join(out))
