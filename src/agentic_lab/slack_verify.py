"""`lab slack verify <owner>`: right after a Slack app's tokens arrive, check them.

Run on the secretary host with sudo (it reads the intake's store). Asks Slack who the bot
is (`auth.test`) and which app it belongs to (`bots.info`), checks the app-level token can
open a Socket Mode connection (`apps.connections.open`), and prints the app's name,
workspace and App ID (for a manager, the line to put in lab.yaml, via the next PR: tools
never write lab.yaml themselves). Tokens are never printed.
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

