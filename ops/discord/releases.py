"""Release time: announce the version, close the loop on bugs tagged fixed-in-next."""
from __future__ import annotations

from collections.abc import Iterator

from . import server
from .api import Discord

EMBED_LIMIT = 4096
COLOR = 0x8B5CF6


def announce_body(tag: str, url: str, notes: str) -> dict:
    if len(notes) > EMBED_LIMIT:
        tail = f"\n\n… full notes: {url}"
        notes = notes[: EMBED_LIMIT - len(tail)].rsplit("\n", 1)[0] + tail
    return {
        "content": f"New version: **{tag}**",
        "embeds": [{
            "title": f"Sessionmate {tag}",
            "url": url,
            "description": notes,
            "color": COLOR,
            "footer": {"text": "Pre-release" if "-" in tag else "Release"},
        }],
    }


def default_channel(tag: str) -> str:
    """Test builds (-rc.) go to the private team channel, the rest to everyone."""
    return "mod-log" if "-rc." in tag else "releases"


def announce(dc: Discord, cfg: dict, tag: str, url: str, notes: str, channel: str | None = None,
             echo=print) -> str | None:
    cid = server.channel_id(cfg, channel or default_channel(tag))
    me = dc.get("/users/@me")["id"]
    title = f"Sessionmate {tag}"
    for m in dc.get(f"/channels/{cid}/messages?limit=30"):
        if m["author"]["id"] == me and any(e.get("title") == title for e in m.get("embeds", [])):
            echo(f"  {tag} already announced (message {m['id']})")
            return None
    msg = dc.post(f"/channels/{cid}/messages", announce_body(tag, url, notes))
    if dc.get(f"/channels/{cid}")["type"] == server.TYPES["announcement"]:
        dc.post(f"/channels/{cid}/messages/{msg['id']}/crosspost")  # reaches servers that follow #releases
    echo(f"  announced {tag} in #{channel or default_channel(tag)}")
    return msg["id"]


def forum_threads(dc: Discord, guild_id: str, forum_id: str) -> Iterator[dict]:
    """Active and archived posts of one forum."""
    for th in dc.get(f"/guilds/{guild_id}/threads/active")["threads"]:
        if th.get("parent_id") == forum_id:
            yield th
    before = ""
    while True:
        page = dc.get(f"/channels/{forum_id}/threads/archived/public?limit=100{before}")
        yield from page["threads"]
        if not page.get("has_more") or not page["threads"]:
            return
        before = "&before=" + page["threads"][-1]["thread_metadata"]["archive_timestamp"]


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
