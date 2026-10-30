"""People's side of the server: roles for what they gave, the roadmap, polls."""
from __future__ import annotations

import datetime
import tomllib

from . import server
from .api import Discord, DiscordError
from .releases import COLOR, forum_threads, tag_ids

# forum key -> (role key, post tags that earn it)
EARNED = {
    "bugs": ("bug-hunter", {"confirmed", "fixed-in-next", "fixed"}),
    "ideas": ("idea-maker", {"planned", "done"}),
}
DONE_SHOWN = 10
POLL_HOURS_MAX = 32 * 24  # Discord: up to 32 days


def award_roles(dc: Discord, cfg: dict, echo=print) -> int:
    """The author of a confirmed bug / a planned idea gets the role. Idempotent: run it as often as you like."""
    gid = cfg["guild"]["id"]
    me = dc.get("/users/@me")["id"]
    given = 0
    for forum_key, (role_key, earning) in EARNED.items():
        role = server.role_id(cfg, role_key)
        if not role:
            continue
        forum = server.channel_id(cfg, forum_key)
        tags = {tid for name, tid in tag_ids(dc, forum).items() if name in earning}
        authors = {th["owner_id"] for th in forum_threads(dc, gid, forum)
                   if tags & set(th.get("applied_tags", [])) and th.get("owner_id") != me}
        for uid in authors:
            try:
                member = dc.get(f"/guilds/{gid}/members/{uid}")
            except DiscordError as exc:
                if exc.status == 404:   # left the server
                    continue
                raise
            if role not in member["roles"]:
                dc.put(f"/guilds/{gid}/members/{uid}/roles/{role}")
                given += 1
                echo(f"  role {role_key} -> {uid}")
    return given


def _private_terms():
    """Where our ideas came from (sources, their brand names): the list lives only in the code repo."""
    try:
        from ops.release import changelog  # noqa: PLC0415 - absent in the public repo copy
    except ImportError:
        raise SystemExit("this command runs from the code repo only: it needs ops/release/hidden_terms.txt") from None
    return changelog


def _post(title: str, lines: list[str]) -> dict:
    return {"content": "", "embeds": [{"title": title, "description": "\n".join(lines), "color": COLOR}],
            "allowed_mentions": {"parse": []}}


def due_week(due: str) -> str:
    """The week the date falls in, Monday to Sunday ("Nov 2–8", "Oct 26 – Nov 1"): no exact release day in public."""
    day = datetime.date.fromisoformat(due)
    monday = day - datetime.timedelta(days=day.weekday())
    sunday = monday + datetime.timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday:%b} {monday.day}–{sunday.day}"
    return f"{monday:%b} {monday.day} – {sunday:%b} {sunday.day}"


def milestone_body(m: dict) -> dict:
    """One milestone = one release: its own post, so a long plan fits Discord's 6000 characters a post."""
    title = f"📍 {m['version']} — {m['title']}" + (f" · 🛠 {m['status']}" if m.get("status") else "")
    if m.get("due"):
        title += f" · 📅 {due_week(m['due'])}"
    return _post(title, [f"• **{i['title']}** — {i['text']}" for i in m["items"]])


def roadmap_body(planned: list[dict], done: list[dict], ideas_id: str) -> dict:
    """The last post: planned ideas, the last done ones. <#thread> renders the post's own title."""
    lines = ["**📅 Planned from your ideas**"]
    lines += [f"• <#{t['id']}>" for t in planned] or ["• nothing yet: vote in the ideas forum"]
    if done:
        lines += ["", "**✅ Done**"] + [f"• <#{t['id']}>" for t in done[:DONE_SHOWN]]
    lines += ["", f"Ideas come from <#{ideas_id}>: add yours, or 👍 the ones you want."]
    return _post("💡 From your ideas", lines)


def milestones(changelog) -> list[dict]:
    """ops/release/roadmap.toml: our own plan, in our own words. A private term there stops the command."""
    path = changelog.HIDDEN.with_name("roadmap.toml")
    with open(path, "rb") as f:
        plan = tomllib.load(f).get("milestone", [])
    words = [w for m in plan for w in (m["version"], m["title"], m.get("status", ""),
                                       *(i["title"] + " " + i["text"] for i in m["items"]))]
    hits = changelog.hidden_hits(" ".join(words), changelog.hidden_terms())
    if hits:
        raise SystemExit(f"{path.name} names where an idea came from: {', '.join(hits)}")
    return plan


def _same(msg: dict, body: dict) -> bool:
    have, want = (msg.get("embeds") or [{}])[0], body["embeds"][0]
    return have.get("title") == want["title"] and have.get("description") == want["description"]


def roadmap(dc: Discord, cfg: dict, echo=print) -> None:
    """Rebuild the roadmap posts: a post per milestone + #ideas tags. Text naming where an idea came from never shows.
    Same number of posts: edited in place. Another number: the bot deletes its posts and posts them again in order."""
    changelog = _private_terms()
    terms = changelog.hidden_terms()
    plan = milestones(changelog)
    gid = cfg["guild"]["id"]
    ideas = server.channel_id(cfg, "ideas")
    tags = tag_ids(dc, ideas)
    planned, done = [], []
    for th in forum_threads(dc, gid, ideas):
        applied = set(th.get("applied_tags", []))
        if changelog.hidden_hits(th["name"], terms):
            continue
        if tags.get("done") in applied:
            done.append(th)
        elif tags.get("planned") in applied:
            planned.append(th)
    done.sort(key=lambda t: int(t["id"]), reverse=True)
    planned.sort(key=lambda t: int(t["id"]))
    posts = [milestone_body(m) for m in plan] + [roadmap_body(planned, done, ideas)]
    channel = server.channel_id(cfg, "roadmap")
    me = dc.get("/users/@me")["id"]
    mine = [m for m in dc.get(f"/channels/{channel}/messages?limit=50") if m["author"]["id"] == me]
    mine.reverse()   # oldest first: the order people read
    if len(mine) == len(posts):
        stale = [(m, body) for m, body in zip(mine, posts, strict=True) if not _same(m, body)]
        if not stale:
            echo("  roadmap up to date")
            return
        for m, body in stale:
            dc.patch(f"/channels/{channel}/messages/{m['id']}", body)
    else:
        for m in mine:
            dc.delete(f"/channels/{channel}/messages/{m['id']}")
        for body in posts:
            dc.post(f"/channels/{channel}/messages", body)
    echo(f"  roadmap: {len(plan)} milestones, {sum(len(m['items']) for m in plan)} items, "
         f"{len(planned)} planned, {len(done)} done")


def poll_body(question: str, answers: list[str], hours: int, multi: bool) -> dict:
    if not 2 <= len(answers) <= 10:
        raise SystemExit("a poll has 2 to 10 answers")
    return {"poll": {"question": {"text": question[:300]},
                     "answers": [{"poll_media": {"text": a[:55]}} for a in answers],
                     "duration": max(1, min(hours, POLL_HOURS_MAX)), "allow_multiselect": multi},
            "allowed_mentions": {"parse": []}}


def poll(dc: Discord, cfg: dict, channel: str, question: str, answers: list[str], hours: int = 72,
         multi: bool = False, echo=print) -> str:
    changelog = _private_terms()
    hits = changelog.hidden_hits(" ".join([question, *answers]), changelog.hidden_terms())
    if hits:
        raise SystemExit(f"the poll names private things: {', '.join(hits)}")
    msg = dc.post(f"/channels/{server.channel_id(cfg, channel)}/messages", poll_body(question, answers, hours, multi))
    echo(f"  poll posted in #{channel} ({hours} h)")
    return msg["id"]
