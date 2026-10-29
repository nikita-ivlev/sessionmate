"""#bugs <-> GitHub Issues of the public repo.

Discord is where reports start: a new #bugs post gets an issue (text only, no name, no files).
GitHub is where status lives: a closed issue tags the post fixed-in-next, a reopened one removes it,
the label "confirmed" tags the post confirmed. The link is a marker in the issue body: no database.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

from . import server
from .api import Discord, DiscordError
from .inbox import thread_url
from .releases import forum_threads, retag, tag_ids

PUBLIC_REPO = "nikita-ivlev/sessionmate"  # = constants.PUBLIC_REPO
LABELS = {"discord": "5865F2", "bug": "d73a4a"}
MARKER = re.compile(r"<!-- discord-thread:(\d+) -->")
QUOTE_LIMIT = 3000


class GitHub:
    def __init__(self, repo: str = PUBLIC_REPO, token: str | None = None) -> None:
        self.repo = repo
        self._token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or _gh_cli_token()

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            "https://api.github.com" + path.replace("{repo}", self.repo),
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Sessionmate-ops"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitHub {method} {path} -> {exc.code}: {exc.read()[:300]!r}") from None

    def issues(self) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self.call("GET", f"/repos/{{repo}}/issues?labels=discord&state=all&per_page=100&page={page}")
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return out
            page += 1


def _gh_cli_token() -> str:
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("No GitHub token: set GH_TOKEN or log in with gh") from None


def defang(text: str) -> str:
    """No @user pings and no #123 cross-links on GitHub from Discord text."""
    text = text.replace("@", "@​")
    return re.sub(r"#(\d)", "#​\\1", text)


def issue_body(guild_id: str, thread: dict, first_text: str) -> str:
    text = defang(first_text.strip() or "(no text)")
    if len(text) > QUOTE_LIMIT:
        text = text[:QUOTE_LIMIT] + " …"
    quote = "\n".join("> " + line for line in text.splitlines())
    return (f"Reported in Discord #bugs: {thread_url(guild_id, thread['id'])}\n\n{quote}\n\n"
            "_Mirrored by the Sessionmate bot. The reporter's name and attached files stay in Discord._\n"
            f"<!-- discord-thread:{thread['id']} -->")


def thread_of(issue: dict) -> str | None:
    m = MARKER.search(issue.get("body") or "")
    return m.group(1) if m else None


def status_moves(issue: dict, applied: list[str], tags: dict[str, str]) -> list[tuple[tuple, tuple, str | None]]:
    """(add, remove, reply) steps that bring a post's tags in line with its issue."""
    nxt, fixed, confirmed = tags["fixed-in-next"], tags["fixed"], tags["confirmed"]
    done = issue["state"] == "closed" and issue.get("state_reason") == "completed"
    moves = []
    if done and nxt not in applied and fixed not in applied:
        moves.append(((nxt,), (), "🛠️ Fixed in the code. The fix ships in the next version."))
    elif issue["state"] == "open" and nxt in applied:
        moves.append(((), (nxt,), "↩️ Reopened: the fix did not hold. We are on it."))
    labels = {lb["name"] for lb in issue.get("labels", [])}
    if "confirmed" in labels and confirmed not in applied and issue["state"] == "open":
        moves.append(((confirmed,), (), None))
    return moves


def sync(dc: Discord, gh: GitHub, cfg: dict, echo=print) -> None:
    gid = cfg["guild"]["id"]
    forum = server.channel_id(cfg, "bugs")
    tags = tag_ids(dc, forum)
    by_thread = {t: i for i in gh.issues() if (t := thread_of(i))}
    _ensure_labels(gh)
    me = dc.get("/users/@me")["id"]
    moved = 0
    for th in forum_threads(dc, gid, forum):
        if th.get("owner_id") == me:
            continue   # the bot's own pinned "how to report" post is not a bug
        issue = by_thread.get(th["id"])
        if issue is None:
            try:  # the starter message id = post id; the author may have deleted it
                text = dc.get(f"/channels/{th['id']}/messages/{th['id']}").get("content", "")
            except DiscordError as exc:
                if exc.status != 404:
                    raise
                text = ""
            issue = gh.call("POST", "/repos/{repo}/issues", {
                "title": th["name"][:200], "body": issue_body(gid, th, text), "labels": list(LABELS)})
            dc.post(f"/channels/{th['id']}/messages",
                    {"content": f"📌 Tracked on GitHub: {issue['html_url']}", "flags": 4})
            echo(f"  new issue #{issue['number']} for post {th['id']}")
            moved += 1
            continue
        for add, remove, say in status_moves(issue, th.get("applied_tags", []), tags):
            retag(dc, th, add=add, remove=remove, say=say)
            echo(f"  post {th['id']} <- issue #{issue['number']}: +{len(add)} -{len(remove)} tags")
            moved += 1
    if not moved:
        echo("  in sync")


def _ensure_labels(gh: GitHub) -> None:
    have = {lb["name"] for lb in gh.call("GET", "/repos/{repo}/labels?per_page=100")}
    for name, color in LABELS.items():
        if name not in have:
            gh.call("POST", "/repos/{repo}/labels", {"name": name, "color": color})
