"""GitHub Apps as agents' GitHub identities: `<app-slug>[bot]` on GitHub, no account to create.

An app acts with short-lived installation tokens (1 hour), minted with its private key. The
key belongs to the agent's service (~/.config/agentic-lab/github-app.pem, unreadable by the
agent); the service mints a token, writes it where the agent's git and gh read it
(TOKEN_DIR: a file git's credential helper and the gh shim read), and mints the next one
before it expires. So the agent always has a working token and never the key.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("agentic_lab.github_app")

API = "https://api.github.com"
KEY = "~/.config/agentic-lab/github-app.pem"         # installed by lab apply (service only)
TOKEN_DIR = "~/.local/share/agentic-lab/github"      # readable by the agent, not writable
REFRESH_BEFORE = dt.timedelta(minutes=10)            # mint the next token this early


class AppError(RuntimeError):
    pass


def app_jwt(app_id, key_pem, now=None):
    """The app's own short JWT (RS256), to ask GitHub for installation tokens."""
    import jwt                                       # PyJWT, with cryptography
    now = int(now or time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, key_pem,
                      algorithm="RS256")


def _call(method, path, bearer, body=None, api=API):
    req = urllib.request.Request(api + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {bearer}",
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "agentic-lab",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except ValueError:
            msg = ""
        raise AppError(f"GitHub {method} {path}: {e.code} {msg}".strip()) from None


def mint(app_id, key_pem, installation_id=None, call=_call):
    """(token, expires_at) for the app's installation (the only one, unless named)."""
    j = app_jwt(app_id, key_pem)
    if installation_id is None:
        inst = call("GET", "/app/installations", j)
        if not inst:
            raise AppError("the app isn't installed anywhere yet: install it on the lab's "
                           "repositories (the app's page → Install App)")
        if len(inst) > 1:
            where = ", ".join(f"{(i.get('account') or {}).get('login', '?')} "
                              f"(installation_id: {i['id']})" for i in inst)
            raise AppError(f"the app is installed in several places: {where}; set the "
                           "installation_id of the right one for it in lab.yaml")
        installation_id = inst[0]["id"]
    t = call("POST", f"/app/installations/{installation_id}/access_tokens", j, body={})
    return t["token"], dt.datetime.fromisoformat(t["expires_at"].replace("Z", "+00:00"))


def check(app_id, key_pem, call=_call, users=None):
    """What an app is: {slug, bot, email, installations: [{id, account, repositories}]}."""
    j = app_jwt(app_id, key_pem)
    app = call("GET", "/app", j)
    slug = app.get("slug")
    inst = [{"id": i["id"], "account": (i.get("account") or {}).get("login"),
             "repositories": i.get("repository_selection")}
            for i in call("GET", "/app/installations", j)]
    bot = f"{slug}[bot]"
    try:                                            # the bot user's id, for its commit email
        uid = (users or _public_user)(bot).get("id")
    except Exception:
        uid = None
    email = (f"{uid}+{bot}@users.noreply.github.com" if uid
             else f"{bot}@users.noreply.github.com")
    return {"slug": slug, "bot": bot, "email": email, "installations": inst}


def _public_user(login):
    req = urllib.request.Request(f"{API}/users/{login}", headers={"User-Agent": "agentic-lab"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def write_token(token, login, token_dir=TOKEN_DIR):
    """Put the token where git's credential helper and gh read it."""
    from .github import write_token as write
    write(token, login, os.path.expanduser(token_dir))


def clock_skew(opener=urllib.request.urlopen):
    """Seconds this host's clock is off from GitHub's (its Date header), or None."""
    import email.utils
    try:
        with opener(urllib.request.Request(API, method="HEAD",
                                           headers={"User-Agent": "agentic-lab"}),
                    timeout=10) as r:
            theirs = email.utils.parsedate_to_datetime(r.headers["Date"])
    except Exception:
        return None
    return (dt.datetime.now(dt.timezone.utc) - theirs).total_seconds()


FAILS_BEFORE_REPORTING = 2


async def keep_token(app_id, login, stop, installation_id=None, key_path=KEY,
                     token_dir=TOKEN_DIR, mint_fn=mint, lister=None, skew_fn=clock_skew):
    """Beside an agent's service: keep a fresh installation token for the agent, and with
    each token, the installation's repositories (repos.txt; a failure keeps the last list)."""
    from .github import list_repos, refresh_repos, write_status
    tdir = os.path.expanduser(token_dir)
    fails, expires = 0, None
    while not stop.is_set():
        wait = 60
        try:
            with open(os.path.expanduser(key_path)) as f:
                key = f.read()
            token, expires = await asyncio.to_thread(mint_fn, app_id, key, installation_id)
            fails = 0
            write_status(tdir, None)
            write_token(token, login, token_dir)
            await asyncio.to_thread(refresh_repos, token, True,
                                    os.path.expanduser(token_dir), lister or list_repos)
            left = (expires - dt.datetime.now(dt.timezone.utc) - REFRESH_BEFORE)
            wait = max(60, left.total_seconds())
        except FileNotFoundError:
            wait = 300                              # the key hasn't been handed over yet
        except Exception as e:
            log.warning("GitHub App token for %s: %s", login, e)
            fails += 1
            if fails >= FAILS_BEFORE_REPORTING:     # #64: visible, not just a log line
                problem = str(e)
                if "401" in problem:
                    skew = await asyncio.to_thread(skew_fn)
                    if skew is not None and abs(skew) > 30:
                        problem += (f" (this host's clock is {abs(skew):.0f} s "
                                    f"{'ahead of' if skew > 0 else 'behind'} GitHub's: fix the "
                                    "clock, e.g. timedatectl set-ntp true)")
                write_status(tdir, f"no token for the GitHub App {login}: {problem}")
                if expires is None or dt.datetime.now(dt.timezone.utc) >= expires:
                    try:                            # an expired token only hides the cause
                        os.remove(os.path.join(tdir, "token"))
                    except OSError:
                        pass
        try:
            await asyncio.wait_for(stop.wait(), wait)
        except asyncio.TimeoutError:
            pass
