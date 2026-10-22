"""GitHub for agents: a manager or worker may have GitHub identities, named in lab.yaml under
`github.accounts`, each either
  - a full bot account (a GitHub user with its own email) and its fine-grained token; or
  - a GitHub App (`app_id`), appearing as `<slug>[bot]`: nothing to create but the app. Its
    tokens last an hour, so the agent's service mints them from the app's private key
    (which the agent can't read) and keeps a fresh one (github_app.py).

An agent has one identity (`github: flash-bot`), or several chosen by repository:

    github:
      acme-flash: default                     # everything else
      spikyclaudebot: [anatoli-starostin/spiky]    # these repositories (owner/* too)

The agent's service writes, where the agent can read but not write (ROOT):
  <identity>/token                               each identity's current token
  <identity>/repos.txt                           the repositories it can reach (#53): an
                                                 App's installation, a bot token's repos
  <identity>/gitconfig                           its commit name and email
  default, repos.tsv                             which identity for which repository
  gitconfig                                      the agent's git config (GIT_CONFIG_GLOBAL):
                                                 the default identity, per-repo includes,
                                                 and the credential helper below
So git picks the identity by the repository (lab-git-credential for the token, an
includeIf for the commit author). gh is a shim first on the agent's PATH (deploy/worker/
gh-shim): it reads the token at call time (an App's changes hourly), the identity mapped
to `-R owner/repo` or the default one, unless the agent set GH_TOKEN itself. gh's own
config (it writes there on many commands) is in the agent's working folder
(<workdir>/.config/gh), never next to the token (#52).

What keeps this safe is GitHub itself: tokens limited to the repositories each identity is
for, and branch protection on `main` (pull requests, reviews by human code owners, no
bypass for bots or apps): identities push branches and open pull requests, issues and
comments, but can't approve or merge.
"""
import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("agentic_lab.github")
API = "https://api.github.com"
REPOS_EVERY_S = 6 * 3600    # a bot account's list: re-read this often (an App's: each token)
MAX_PAGES = 10              # 1000 repositories is plenty for a prompt

SECRETS = "~/.config/agentic-lab/github"     # <name>.token / <name>.pem (service only)
ROOT = "~/.local/share/agentic-lab/github"   # readable by the agent, not writable
# the secretary's identity is its service's alone (config repo PRs): kept in its state
# folder, which its agent's sandbox can't read
SERVICE_ROOT = "~/.local/state/agentic-lab/github"
HELPER = "/opt/agentic-lab/src/deploy/worker/lab-git-credential"
SHIM = "/opt/agentic-lab/src/deploy/worker/gh-shim"      # its `gh`, first on the PATH
DOMAINS = ["github.com", "api.github.com", "codeload.github.com", "uploads.github.com",
           "objects.githubusercontent.com", "raw.githubusercontent.com"]


def _write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_token(token, login, identity_dir):
    """An identity's token, where the credential helper and the gh shim read it."""
    _write(os.path.join(identity_dir, "token"), token + "\n")
    try:                                # gh's old copy of it (before #52)
        os.remove(os.path.join(identity_dir, "gh", "hosts.yml"))
    except OSError:
        pass


def list_repos(token, app, opener=None):
    """The repositories a token reaches, as sorted `owner/repo`: an App installation's
    (GET /installation/repositories) or a bot account's (GET /user/repos). Raises on any
    failure: callers keep the last list."""
    opener = opener or urllib.request.urlopen
    names = []
    for page in range(1, MAX_PAGES + 1):
        url = (f"{API}/installation/repositories?per_page=100&page={page}" if app else
               f"{API}/user/repos?per_page=100&page={page}"
               "&affiliation=owner,collaborator,organization_member")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agentic-lab"})
        with opener(req, timeout=20) as r:
            data = json.loads(r.read())
        batch = data.get("repositories", []) if app else data
        names += [x["full_name"] for x in batch if x.get("full_name")]
        if len(batch) < 100:
            break
    return sorted(set(names), key=str.lower)


def write_repos(identity_dir, repos):
    """<identity>/repos.txt: a dated header, then one owner/repo per line."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    _write(os.path.join(identity_dir, "repos.txt"),
           f"# repositories this identity can reach, as of {now}\n"
           + "".join(f"{r}\n" for r in repos))


def read_repos(identity_dir):
    """(repos, as of) from <identity>/repos.txt, or (None, None) if there's none yet."""
    try:
        with open(os.path.join(identity_dir, "repos.txt")) as f:
            lines = f.read().splitlines()
    except OSError:
        return None, None
    when = lines[0].rsplit("as of ", 1)[-1] if lines and lines[0].startswith("#") else None
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")], when


def write_status(identity_dir, problem):
    """<identity>/status: why the identity doesn't work right now (#64), or removed when it
    does. The agent's prompt, the gh shim, health checks and the secretary show it."""
    path = os.path.join(identity_dir, "status")
    if problem:
        _write(path, problem.strip() + "\n")
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def read_status(identity_dir):
    try:
        with open(os.path.join(identity_dir, "status")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def problems(lab, agent_id, root=ROOT):
    """{identity: problem} for the agent's GitHub identities that don't work right now."""
    root = os.path.expanduser(root)
    out = {}
    for name in lab.github_names(agent_id):
        p = read_status(os.path.join(root, name))
        if p:
            out[name] = p
    return out


def refresh_repos(token, app, identity_dir, lister=list_repos):
    """List and write an identity's repositories; on any failure keep the last list (the
    agent's GitHub access never depends on this)."""
    try:
        write_repos(identity_dir, lister(token, app))
        if not app:
            write_status(identity_dir, None)        # the bot's token works
        return True
    except urllib.error.HTTPError as e:
        if not app and e.code == 401:               # #64: say so, don't just fail later
            write_status(identity_dir, "its token was rejected (expired or revoked): an "
                         "admin must hand over a new one (lab secret request "
                         f"service:github-{os.path.basename(identity_dir).lower()} github-token)")
        log.warning("listing the repositories of %s: %s", os.path.basename(identity_dir), e)
        return False
    except Exception as e:                  # no network, revoked token, rate limit...
        log.warning("listing the repositories of %s: %s", os.path.basename(identity_dir), e)
        return False


async def keep_bot_repos(name, stop, token_dir, lister=list_repos, every=REPOS_EVERY_S):
    """A bot account's repositories, now and every few hours (its token is in its folder)."""
    import asyncio
    while not stop.is_set():
        try:
            with open(os.path.join(token_dir, "token")) as f:
                token = f.read().strip()
            await asyncio.to_thread(refresh_repos, token, False, token_dir, lister)
        except OSError:
            pass                            # no token handed over yet
        try:
            await asyncio.wait_for(stop.wait(), every)
        except asyncio.TimeoutError:
            pass


def prepare(lab, agent_id, root=ROOT, secrets=SECRETS, helper=HELPER, shim=SHIM):
    """Write the agent's git setup (and bot accounts' tokens). Returns its environment:
    empty for an agent without GitHub."""
    default, mapped = lab.github_identities(agent_id)
    if not default:
        return {}
    root, secrets = os.path.expanduser(root), os.path.expanduser(secrets)
    for name in lab.github_names(agent_id):
        d = os.path.join(root, name)
        _write(os.path.join(d, "gitconfig"), f"[user]\n\tname = {lab.github_login(name)}\n"
                                             f"\temail = {lab.github_email(name)}\n")
        if not lab.github_is_app(name):              # an app's token comes from keep_token
            try:
                with open(os.path.join(secrets, f"{name}.token")) as f:
                    write_token(f.read().strip(), lab.github_login(name), d)
            except OSError:
                pass                                 # not handed over yet
    _write(os.path.join(root, "default"), default + "\n")
    _write(os.path.join(root, "repos.tsv"), "".join(f"{p}\t{n}\n" for p, n in mapped))
    includes = "".join(
        f'[includeIf "hasconfig:remote.*.url:https://github.com/{p}{suffix}"]\n'
        f"\tpath = {os.path.join(root, n, 'gitconfig')}\n"
        for p, n in mapped for suffix in ("", ".git"))
    _write(os.path.join(root, "gitconfig"),
           f"[include]\n\tpath = {os.path.join(root, default, 'gitconfig')}\n"
           f'[credential "https://github.com"]\n\thelper = {helper}\n\tuseHttpPath = true\n'
           + includes)
    # gh writes its config (migrations, state): a folder of its own the sandbox lets it
    # write, with no secrets in it
    workdir = os.path.expanduser((lab.agents.get(agent_id) or {}).get("workdir") or "~/work")
    gh_config = os.path.join(workdir, ".config", "gh")
    try:                                # in the agent's folder: never through a link (safefs)
        from . import safefs
        os.makedirs(workdir, exist_ok=True)
        safefs.makedirs(workdir, os.path.join(".config", "gh"))
    except OSError:                     # gh makes it itself; never lose git's setup over it
        pass
    return {"GIT_CONFIG_GLOBAL": os.path.join(root, "gitconfig"),
            "GH_CONFIG_DIR": gh_config,
            "LAB_GITHUB_DIR": root,
            "PATH": shim + ":" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


def env_for(lab, agent_id, root=ROOT, secrets=SECRETS):
    """The agent's environment for GitHub (writing its git setup first)."""
    try:
        return prepare(lab, agent_id, root=root, secrets=secrets)
    except OSError:
        return {}


def start(lab, agent_id, stop, root=ROOT, secrets=SECRETS):
    """The tasks beside the agent's service: its GitHub App identities' tokens kept fresh,
    and every identity's reachable repositories (repos.txt) listed."""
    import asyncio

    from .github_app import keep_token
    tasks = []
    for name in lab.github_names(agent_id):
        if not lab.github_is_app(name):
            tasks.append(asyncio.create_task(keep_bot_repos(
                name, stop, os.path.join(os.path.expanduser(root), name))))
            continue
        acc = lab.github_accounts[name]
        tasks.append(asyncio.create_task(keep_token(
            acc["app_id"], lab.github_login(name), stop,
            installation_id=acc.get("installation_id"),
            key_path=os.path.join(os.path.expanduser(secrets), f"{name}.pem"),
            token_dir=os.path.join(os.path.expanduser(root), name))))
    return tasks


def instructions(lab, agent_id, root=ROOT):
    """The GitHub paragraph of the agent's system prompt. Read at the start of every session
    (repos.txt changes when an App's installation does), so it's never stale for long."""
    default, mapped = lab.github_identities(agent_id)
    if not default:
        return ""
    root = os.path.expanduser(root)

    def who(name):
        kind = "the GitHub App" if lab.github_is_app(name) else "the bot account"
        return f"{kind} {lab.github_login(name)}"

    def reach(name):
        repos, when = read_repos(os.path.join(root, name))
        if repos is None:
            return (f"(its list isn't there yet: read {root}/{name}/repos.txt later, or try "
                    "the exact owner/repo you were given)")
        if not repos:
            return "(it can reach no repository yet: an admin must give it access)"
        shown = ", ".join(repos[:60]) + (f", and {len(repos) - 60} more" if len(repos) > 60
                                         else "")
        return f"can reach: {shown} (as of {when})"
    text = f"\nGitHub: you act as {who(default)}; it {reach(default)}."
    for name, problem in problems(lab, agent_id, root).items():
        text += (f" ⚠ {who(name)} doesn't work right now: {problem}. Tell the member (an "
                 "admin must fix it); don't conclude the repository is gone.")
    if mapped:
        rows = "; ".join(f"{p}: {who(n)}" for p, n in mapped)
        text += (f" For some repositories you act as another identity: {rows}. "
                 + " ".join(f"{who(n)} {reach(n)}." for n in dict.fromkeys(n for _, n in mapped)))
    text += (f" The current lists are in {root}/<identity>/repos.txt. Use exact owner/repo "
             "names from them: never guess an owner (names are case-insensitive, but a guessed "
             "owner is a different account). Clone with git clone "
             "https://github.com/<owner>/<repo>.git: git picks the identity by itself, so do "
             "commits (their author). gh picks it too, from -R owner/repo, a repository named in "
             "the command (gh repo view owner/repo, gh api repos/owner/repo/...) or the clone "
             f"you're in (LAB_GH_DEBUG=1 shows which). {root}/repos.tsv lists only repositories mapped to an identity other than "
             f"the default; empty means one identity for everything, not \"no access\". If a "
             "repository isn't in any list, your identities can't reach it: say so (an admin "
             "can give access), don't conclude you have no GitHub access.")
    return text + (" Push feature branches and open pull requests; issues and comments are "
                   "fine. Never push to main, never approve or merge a pull request: humans "
                   "review and merge. Never print or share a token.\n")
