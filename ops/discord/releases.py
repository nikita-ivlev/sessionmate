"""Release time: announce the version, close the loop on bugs tagged fixed-in-next."""
from __future__ import annotations

import re

from . import server
from .api import Discord

EMBED_LIMIT = 4096
COLOR = 0xFF5E00  # brand orange
LOGO = "https://raw.githubusercontent.com/nikita-ivlev/sessionmate/main/assets/logo.png"
PING_ROLE = "release-pings"  # role key in server.toml: members pick it in onboarding


def announce_body(tag: str, url: str, notes: str, ping: str | None = None, download: str | None = None) -> dict:
    """notes = the CHANGELOG section of this version (markdown); download = the Setup.exe asset URL."""
    notes = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*$", r"**\1**", notes.strip())  # headings -> bold: one size in an embed
    head = f"⬇️ **[Download {download.rsplit('/', 1)[-1]}]({download})** · Windows 10/11\n\n" if download else ""
    tail = f"\n\n[Full release notes]({url})"
    room = EMBED_LIMIT - len(head) - len(tail)
    if len(notes) > room:
        notes = notes[: room - 2].rsplit("\n", 1)[0] + "\n…"
    content = f"New version: **{tag}**"
    if ping:
        content = f"<@&{ping}> {content}"
    buttons = [{"type": 2, "style": 5, "label": "Download for Windows", "url": download, "emoji": {"name": "⬇️"}}]
    buttons = (buttons if download else []) + [{"type": 2, "style": 5, "label": "Release notes", "url": url}]
    return {
        "content": content,
        "allowed_mentions": {"roles": [ping] if ping else []},
        "embeds": [{
            "title": f"Sessionmate {tag}",
            "url": url,
            "description": head + notes + tail,
            "color": COLOR,
            "thumbnail": {"url": LOGO},
            "footer": {"text": "Pre-release" if "-" in tag else "Release"},
        }],
        "components": [{"type": 1, "components": buttons}],  # link buttons: one click to the installer
    }


def default_channel(tag: str) -> str:
    """Test builds (-rc.) go to the private team channel, the rest to everyone."""
    return "mod-log" if "-rc." in tag else "releases"


def announce(dc: Discord, cfg: dict, tag: str, url: str, notes: str, channel: str | None = None,
             echo=print, download: str | None = None) -> str | None:
    _no_private_terms(notes)
    cid = server.channel_id(cfg, channel or default_channel(tag))
    me = dc.get("/users/@me")["id"]
    title = f"Sessionmate {tag}"
    for m in dc.get(f"/channels/{cid}/messages?limit=30"):
        if m["author"]["id"] == me and any(e.get("title") == title for e in m.get("embeds", [])):
            echo(f"  {tag} already announced (message {m['id']})")
            return None
    ping = None if "-rc." in tag else server.role_id(cfg, PING_ROLE)  # test builds ping nobody
    msg = dc.post(f"/channels/{cid}/messages", announce_body(tag, url, notes, ping, download))
    if dc.get(f"/channels/{cid}")["type"] == server.TYPES["announcement"]:
        dc.post(f"/channels/{cid}/messages/{msg['id']}/crosspost")  # reaches servers that follow #releases
    echo(f"  announced {tag} in #{channel or default_channel(tag)}")
    return msg["id"]


def _no_private_terms(text: str) -> None:
    """No public text says where an idea came from. The list lives only in the code repo."""
    try:
        from ops.release import changelog  # noqa: PLC0415 - absent in the public repo copy
    except ImportError:
        return
    hits = changelog.hidden_hits(text, changelog.hidden_terms())
    if hits:
        raise SystemExit(f"announcement names private things: {', '.join(hits)}")


forum_threads = server.forum_threads  # inbox and issues import it from here


def tag_ids(dc: Discord, forum_id: str) -> dict[str, str]:
    return {t["name"]: t["id"] for t in dc.get(f"/channels/{forum_id}")["available_tags"]}


def retag(dc: Discord, th: dict, add: tuple[str, ...] = (), remove: tuple[str, ...] = (),
          say: str | None = None) -> None:
    """Change a post's tags (ids) and optionally reply. An archived post is reopened and archived back."""
    archived = th["thread_metadata"]["archived"]
    tags = [t for t in th.get("applied_tags", []) if t not in remove]
    tags += [t for t in add if t not in tags]
    body: dict = {"applied_tags": tags[-5:]}  # Discord allows 5 tags per post
    if archived:
        body["archived"] = False
    dc.patch(f"/channels/{th['id']}", body)
    th["applied_tags"] = body["applied_tags"]
    if say:
        dc.post(f"/channels/{th['id']}/messages", {"content": say, "flags": 4})
    if archived:
        dc.patch(f"/channels/{th['id']}", {"archived": True})


def mark_fixed(dc: Discord, cfg: dict, tag: str, url: str, echo=print) -> list[str]:
    """fixed-in-next -> fixed, and tell the reporter which version has the fix."""
    forum = server.channel_id(cfg, "bugs")
    tags = tag_ids(dc, forum)
    nxt, done = tags["fixed-in-next"], tags["fixed"]
    changed = []
    for th in forum_threads(dc, cfg["guild"]["id"], forum):
        if nxt not in th.get("applied_tags", []):
            continue
        retag(dc, th, add=(done,), remove=(nxt,), say=f"✅ Fixed in **{tag}**: {url}")
        changed.append(th["id"])
        echo(f"  fixed: {th['id']} {th['name']}")
    if not changed:
        echo("  no posts tagged fixed-in-next")
    return changed
