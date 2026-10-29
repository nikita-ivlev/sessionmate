"""Server as code: server.toml <-> the live Discord server.

apply only creates and changes. Things on the server that the file does not list are reported;
they are deleted only with prune=True (a human decides that). Two exceptions, both bot-owned:
the bot's own posts in a channel with [[message]] specs are rebuilt when their number or order
changes, and the onboarding / welcome screen are replaced as a whole.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import re
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .api import Discord, DiscordError

CONFIG = Path(__file__).with_name("server.toml")

TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5, "forum": 15}
TYPE_NAMES = {v: k for k, v in TYPES.items()}

VIEW = 1 << 10
SEND = 1 << 11
PUBLIC_THREADS = 1 << 35
PRIVATE_THREADS = 1 << 36
SEND_IN_THREADS = 1 << 38
ACCESS_DENY = {
    "public": 0,
    "read-only": SEND | SEND_IN_THREADS | PUBLIC_THREADS | PRIVATE_THREADS,
    "private": VIEW,
}

PERMS = {
    "KICK_MEMBERS": 1 << 1, "BAN_MEMBERS": 1 << 2, "ADMINISTRATOR": 1 << 3, "MANAGE_CHANNELS": 1 << 4,
    "MANAGE_GUILD": 1 << 5, "ADD_REACTIONS": 1 << 6, "VIEW_AUDIT_LOG": 1 << 7, "VIEW_CHANNEL": VIEW,
    "SEND_MESSAGES": SEND, "MANAGE_MESSAGES": 1 << 13, "EMBED_LINKS": 1 << 14, "ATTACH_FILES": 1 << 15,
    "MENTION_EVERYONE": 1 << 17, "MANAGE_NICKNAMES": 1 << 27, "MANAGE_ROLES": 1 << 28,
    "MANAGE_WEBHOOKS": 1 << 29, "MANAGE_GUILD_EXPRESSIONS": 1 << 30, "MANAGE_EVENTS": 1 << 33,
    "MANAGE_THREADS": 1 << 34, "MODERATE_MEMBERS": 1 << 40,
}

GUILD_FIELDS = ("name", "description", "preferred_locale", "verification_level",
                "explicit_content_filter", "default_message_notifications")
GUILD_CHANNELS = ("rules_channel", "public_updates_channel", "safety_alerts_channel", "system_channel")
MESSAGE_LIMIT = 2000
NO_PINGS = {"parse": []}  # a post may show a role or a channel; it never pings anyone
LAYOUTS = {"list": 1, "gallery": 2}  # forum view
SORTS = {"activity": 0, "created": 1}
# AutoMod: only "keyword" rules can be several; one of each other kind per server, so those match by kind.
TRIGGERS = {"keyword": 1, "spam": 3, "preset": 4, "mention_spam": 5}
PRESETS = {"profanity": 1, "sexual": 2, "slurs": 3}
PINNED = 1 << 1  # forum post flag


# --- config -------------------------------------------------------------------------------------

def load(path: Path = CONFIG) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base"] = path.parent  # images are relative to the file
    for msg in cfg.get("message", []):
        if len(msg.get("text", "")) > MESSAGE_LIMIT:
            raise SystemExit(f"message in #{msg['channel']}: {len(msg['text'])} chars, limit {MESSAGE_LIMIT}")
    return cfg


def key(spec: dict) -> str:
    """The stable short name code uses ("bugs"); the display name may carry an emoji ("🐛┃bugs")."""
    return spec.get("key", spec["name"])


def channel_specs(cfg: dict) -> list[tuple[dict, dict]]:
    """(category spec, channel spec) in the order of the file."""
    return [(cat, ch) for cat in cfg.get("category", []) for ch in cat.get("channel", [])]


def channel_spec(cfg: dict, k: str) -> dict:
    for _, ch in channel_specs(cfg):
        if key(ch) == k:
            return ch
    raise SystemExit(f"server.toml: no channel {k}")


def channel_id(cfg: dict, k: str) -> str:
    ch = channel_spec(cfg, k)
    if not ch.get("id"):
        raise SystemExit(f"server.toml: channel {k} has no id (run pin-ids)")
    return ch["id"]


def role_id(cfg: dict, k: str) -> str | None:
    return next((r.get("id") for r in cfg.get("role", []) if key(r) == k), None)


def image_name(path: Path) -> str:
    """Content hash in the name: apply sees a changed picture by the attachment's file name."""
    return f"{path.stem}-{hashlib.sha256(path.read_bytes()).hexdigest()[:8]}{path.suffix}"


def data_uri(path: Path) -> str:
    return f"data:image/{path.suffix.lstrip('.')};base64," + base64.b64encode(path.read_bytes()).decode()


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


# --- live state ---------------------------------------------------------------------------------

@dataclass
class Live:
    guild: dict
    channels: list[dict]
    roles: list[dict]
    bot_id: str
    members: dict[str, dict] = field(default_factory=dict)
    posts: dict[str, list[dict]] = field(default_factory=dict)  # channel key -> bot posts, oldest first
    emojis: list[dict] = field(default_factory=list)
    onboarding: dict | None = None
    welcome: dict | None = None
    automod: list[dict] = field(default_factory=list)
    threads: dict[str, list[dict]] = field(default_factory=dict)  # forum key -> the bot's own posts

    @classmethod
    def fetch(cls, dc: Discord, cfg: dict) -> Live:
        gid = cfg["guild"]["id"]
        live = cls(
            guild=dc.get(f"/guilds/{gid}"),
            channels=dc.get(f"/guilds/{gid}/channels"),
            roles=dc.get(f"/guilds/{gid}/roles"),
            bot_id=dc.get("/users/@me")["id"],
            emojis=dc.get(f"/guilds/{gid}/emojis"),
        )
        for uid in {u for r in cfg.get("role", []) for u in r.get("members", [])}:
            live.members[uid] = dc.get(f"/guilds/{gid}/members/{uid}")
        for k in dict.fromkeys(m["channel"] for m in cfg.get("message", [])):
            ch = live.channel(channel_spec(cfg, k))
            if ch:
                mine = [m for m in dc.get(f"/channels/{ch['id']}/messages?limit=100")
                        if m["author"]["id"] == live.bot_id]
                live.posts[k] = sorted(mine, key=lambda m: int(m["id"]))
        if "onboarding" in cfg:
            live.onboarding = dc.get(f"/guilds/{gid}/onboarding")
        if "welcome_screen" in cfg:
            live.welcome = dc.get_or_none(f"/guilds/{gid}/welcome-screen")
        if "automod" in cfg:
            live.automod = dc.get(f"/guilds/{gid}/auto-moderation/rules")
        for k in dict.fromkeys(p["channel"] for p in cfg.get("forum_post", [])):
            forum = live.channel(channel_spec(cfg, k))
            if forum:
                live.threads[k] = [t for t in forum_threads(dc, gid, forum["id"]) if t.get("owner_id") == live.bot_id]
                for th in live.threads[k]:  # the starter message id = the post id
                    starter = dc.get_or_none(f"/channels/{th['id']}/messages/{th['id']}")
                    th["_text"] = (starter or {}).get("content", "")
        return live

    def channel(self, spec: dict, category: bool = False) -> dict | None:
        """A text channel and a forum are different things: Discord turns text into announcement
        and back, nothing else. A channel of another type does not match (apply makes a new one)."""
        pool = [c for c in self.channels if (c["type"] == 4) == category]
        if not category and spec.get("type"):
            want = TYPES[spec["type"]]
            ok = {0, 5} if want in (0, 5) else {want}
            pool = [c for c in pool if c["type"] in ok]
        by_id = next((c for c in pool if spec.get("id") and c["id"] == spec["id"]), None)
        return by_id or next((c for c in pool if c["name"] in (spec["name"], key(spec))), None)

    def role(self, spec: dict) -> dict | None:
        by_id = next((r for r in self.roles if spec.get("id") and r["id"] == spec["id"]), None)
        return by_id or next((r for r in self.roles if r["name"] == spec["name"]), None)

    def refs(self, cfg: dict) -> dict[str, dict[str, str]]:
        """key -> live id for channels, roles and emojis: what {#..} {@..} {:..} in texts point at."""
        chans = {key(ch): c["id"] for _, ch in channel_specs(cfg) if (c := self.channel(ch))}
        roles = {key(r): x["id"] for r in cfg.get("role", []) if (x := self.role(r))}
        emojis = {e["name"]: e["id"] for e in self.emojis}
        return {"#": chans, "@": roles, ":": emojis}


# --- text helpers -------------------------------------------------------------------------------

def render(text: str, refs: dict[str, dict[str, str]], planned: dict[str, set[str]] | None = None) -> str:
    """{#bugs} -> channel link, {@release-pings} -> role, {:sessionmate} -> server emoji.

    planned: keys server.toml has but the server not yet (a dry run before they exist) -> plain text.
    """
    fmt = {"#": "<#{}>", "@": "<@&{}>", ":": "<:{name}:{}>"}

    def sub(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2)
        if name in refs.get(kind, {}):
            return fmt[kind].format(refs[kind][name], name=name)
        if planned and name in planned.get(kind, set()):
            return f"{kind}{name}"
        raise SystemExit(f"unknown {{{kind}{name}}} in a message")
    return re.sub(r"\{([#@:])([\w-]+)\}", sub, text)


def unrender(text: str, ids: dict[str, str]) -> str:
    return re.sub(r"<#(\d+)>", lambda m: "{#" + ids.get(m.group(1), m.group(1)) + "}", text)


def access_of(ch: dict, everyone: str) -> str:
    ow = next((o for o in ch.get("permission_overwrites", []) if o["id"] == everyone), None)
    deny = int(ow["deny"]) if ow else 0
    if deny & VIEW:
        return "private"
    if deny & SEND:
        return "read-only"
    return "public"


def perms_bits(names: list[str]) -> int:
    bits = 0
    for n in names:
        if n not in PERMS:
            raise SystemExit(f"unknown permission {n}")
        bits |= PERMS[n]
    return bits


def perms_names(bits: int) -> list[str]:
    out = [n for n, b in PERMS.items() if bits & b]
    known = perms_bits(out)
    out += [f"BIT_{i}" for i in range(64) if bits & (1 << i) and not known & (1 << i)]
    return out


def _tags_now(ch: dict) -> list[tuple]:
    return [(t["name"], bool(t.get("moderated")), t.get("emoji_name")) for t in ch.get("available_tags", [])]


def _tags_body(spec_tags: list[dict], ch: dict, prune: bool) -> list[dict]:
    old = {t["name"]: t for t in ch.get("available_tags", [])}
    body = []
    for t in spec_tags:
        tag = {"name": t["name"], "moderated": bool(t.get("moderated", False))}
        if t.get("emoji"):
            tag["emoji_name"] = t["emoji"]
        if t["name"] in old:
            tag["id"] = old[t["name"]]["id"]
        body.append(tag)
    if not prune:  # removing a tag strips it from every post, so only with prune
        wanted = {t["name"] for t in spec_tags}
        body += [{k: t[k] for k in ("id", "name", "moderated", "emoji_name") if t.get(k) is not None}
                 for t in ch.get("available_tags", []) if t["name"] not in wanted]
    return body


def _tags_cmp(body: list[dict]) -> list[tuple]:
    return [(t["name"], bool(t.get("moderated")), t.get("emoji_name")) for t in body]


_ids = itertools.count()


def snowflake() -> str:
    """A fresh id for new onboarding prompts and options (Discord wants the client to pick one)."""
    return str(((int(time.time() * 1000) - 1420070400000) << 22) + next(_ids))


# --- plan ---------------------------------------------------------------------------------------

@dataclass
class Change:
    desc: str
    method: str
    path: str
    body: object = None
    files: list[tuple[str, bytes]] | None = None
    if_missing: Change | None = None   # run this instead when the target answers 404


def plan_parents(cfg: dict, live: Live) -> list[Change]:
    gid = cfg["guild"]["id"]
    out = []
    for spec in cfg.get("role", []):
        if not live.role(spec):
            out.append(Change(f"create role {spec['name']}", "POST", f"/guilds/{gid}/roles", {"name": spec["name"]}))
    for cat in cfg.get("category", []):
        if not live.channel(cat, category=True):
            out.append(Change(f"create category {cat['name']}", "POST", f"/guilds/{gid}/channels",
                              {"name": cat["name"], "type": 4}))
    have = {e["name"] for e in live.emojis}
    for e in cfg.get("emoji", []):
        if e["name"] not in have:
            out.append(Change(f"create emoji :{e['name']}:", "POST", f"/guilds/{gid}/emojis",
                              {"name": e["name"], "image": data_uri(cfg["_base"] / e["image"]), "roles": []}))
    return out


def plan_channels(cfg: dict, live: Live) -> list[Change]:
    gid = cfg["guild"]["id"]
    out = []
    for cat, spec in channel_specs(cfg):
        if live.channel(spec):
            continue
        parent = live.channel(cat, category=True)
        kind = spec.get("type", "text")
        body = {"name": spec["name"], "type": TYPES["text" if kind == "announcement" else kind]}
        if parent:
            body["parent_id"] = parent["id"]
        out.append(Change(f"create #{spec['name']} ({kind}) in {cat['name']}", "POST",
                          f"/guilds/{gid}/channels", body))
    return out


def plan_updates(cfg: dict, live: Live, prune: bool = False) -> list[Change]:
    gid = cfg["guild"]["id"]
    out: list[Change] = []
    refs = live.refs(cfg)

    # guild first: Community must be on before a channel can become an announcement channel
    g = cfg.get("guild", {})
    body = {k: g[k] for k in GUILD_FIELDS if k in g and live.guild.get(k) != g[k]}
    for k in GUILD_CHANNELS:
        want = refs["#"].get(g.get(k, ""))
        if want and live.guild.get(f"{k}_id") != want:
            body[f"{k}_id"] = want
    if g.get("community") and "COMMUNITY" not in live.guild.get("features", []):
        body["features"] = sorted(set(live.guild.get("features", [])) | {"COMMUNITY"})
    if body:
        out.append(Change(f"guild: {', '.join(sorted(body))}", "PATCH", f"/guilds/{gid}", body))

    out += _plan_roles(cfg, live)
    for cat, spec in channel_specs(cfg):
        out += _plan_channel(cfg, live, cat, spec, prune)
    out += _plan_positions(cfg, live)
    out += plan_posts(cfg, live, refs)
    out += plan_forum_posts(cfg, live, refs)
    out += _plan_onboarding(cfg, live, refs)
    out += _plan_welcome_screen(cfg, live, refs)
    out += plan_automod(cfg, live, refs)
    return out


def _automod_body(spec: dict, refs: dict) -> dict:
    kind = spec["trigger"]
    meta: dict = {}
    if kind == "keyword":
        meta = {"keyword_filter": spec.get("keywords", []), "regex_patterns": spec.get("regex", []),
                "allow_list": spec.get("allow", [])}
    elif kind == "preset":
        meta = {"presets": [PRESETS[p] for p in spec["presets"]], "allow_list": spec.get("allow", [])}
    elif kind == "mention_spam":
        meta = {"mention_total_limit": spec["mention_limit"],
                "mention_raid_protection_enabled": spec.get("raid_protection", True)}
    block: dict = {"type": 1, "metadata": {}}   # the message is not sent; no ban, no timeout
    if spec.get("message"):
        block["metadata"]["custom_message"] = spec["message"]
    actions = [block]
    if spec.get("alert") in refs["#"]:
        actions.append({"type": 2, "metadata": {"channel_id": refs["#"][spec["alert"]]}})
    return {"name": spec["name"], "event_type": 1, "trigger_type": TRIGGERS[kind], "trigger_metadata": meta,
            "actions": actions, "enabled": spec.get("enabled", True),
            "exempt_roles": sorted(refs["@"][r] for r in spec.get("exempt_roles", []) if r in refs["@"])}


def _automod_view(rule: dict) -> tuple:
    meta = rule.get("trigger_metadata") or {}
    norm = {k: sorted(v) if isinstance(v, list) else v for k, v in meta.items()}
    acts = sorted((a["type"], a.get("metadata", {}).get("custom_message", ""), a.get("metadata", {}).get("channel_id", ""))
                  for a in rule.get("actions", []))
    return rule["name"], norm, acts, bool(rule.get("enabled")), sorted(rule.get("exempt_roles", []))


def plan_automod(cfg: dict, live: Live, refs: dict) -> list[Change]:
    """AutoMod blocks a message and tells #mod-log; it never bans or times anyone out."""
    gid = cfg["guild"]["id"]
    out = []
    for spec in cfg.get("automod", []):
        body = _automod_body(spec, refs)
        kind = body["trigger_type"]
        rule = next((r for r in live.automod if r["trigger_type"] == kind and (kind != 1 or r["name"] == spec["name"])),
                    None)
        if rule is None:
            out.append(Change(f"create automod rule {spec['name']}", "POST", f"/guilds/{gid}/auto-moderation/rules", body))
            continue
        live_view = _automod_view(rule)
        want_view = _automod_view(body)
        keys = set(want_view[1])   # Discord adds metadata keys we do not set: compare ours only
        live_view = (live_view[0], {k: live_view[1].get(k, [] if isinstance(want_view[1][k], list) else None)
                                    for k in keys}, *live_view[2:])
        if live_view != want_view:
            patch = {k: v for k, v in body.items() if k != "trigger_type"}   # the kind of a rule cannot change
            # Discord's own default rule is a template until saved: PATCH says 404, a new rule replaces it.
            create = Change(f"create automod rule {spec['name']}", "POST", f"/guilds/{gid}/auto-moderation/rules", body)
            out.append(Change(f"automod rule {spec['name']}", "PATCH",
                              f"/guilds/{gid}/auto-moderation/rules/{rule['id']}", patch, if_missing=create))
    return out


def plan_forum_posts(cfg: dict, live: Live, refs: dict) -> list[Change]:
    """[[forum_post]]: the bot's own post in a forum (FAQ), matched by its title; pinned on request."""
    out = []
    for spec in cfg.get("forum_post", []):
        k = spec["channel"]
        forum = live.channel(channel_spec(cfg, k))
        if not forum or k not in live.threads:
            continue
        text = render(spec["text"], refs)
        th = next((t for t in live.threads[k] if t["name"] == spec["title"]), None)
        if th is None:
            tags = {t["name"]: t["id"] for t in forum.get("available_tags", [])}
            out.append(Change(f"#{k}: new post {spec['title']!r}", "POST", f"/channels/{forum['id']}/threads", {
                "name": spec["title"], "applied_tags": [tags[t] for t in spec.get("tags", []) if t in tags],
                "message": {"content": text, "flags": 4, "allowed_mentions": NO_PINGS}}))
            continue
        if th["thread_metadata"].get("archived") and (th["_text"] != text or spec.get("pinned")):
            out.append(Change(f"#{k}: reopen post {spec['title']!r}", "PATCH", f"/channels/{th['id']}",
                              {"archived": False}))
        if th["_text"] != text:
            out.append(Change(f"#{k}: edit post {spec['title']!r}", "PATCH", f"/channels/{th['id']}/messages/{th['id']}",
                              {"content": text}))
        if spec.get("pinned") and not th.get("flags", 0) & PINNED:
            out.append(Change(f"#{k}: pin post {spec['title']!r}", "PATCH", f"/channels/{th['id']}",
                              {"flags": th.get("flags", 0) | PINNED}))
    return out


def _plan_roles(cfg: dict, live: Live) -> list[Change]:
    gid = cfg["guild"]["id"]
    out = []
    for spec in cfg.get("role", []):
        role = live.role(spec)
        if not role:
            continue
        rb = {}
        if spec.get("id") and role["name"] != spec["name"]:
            rb["name"] = spec["name"]
        if "color" in spec and role["color"] != int(spec["color"].lstrip("#"), 16):
            rb["color"] = int(spec["color"].lstrip("#"), 16)
        for k in ("hoist", "mentionable"):
            if k in spec and role[k] != spec[k]:
                rb[k] = spec[k]
        if "permissions" in spec and int(role["permissions"]) != perms_bits(spec["permissions"]):
            rb["permissions"] = str(perms_bits(spec["permissions"]))
        if rb:
            out.append(Change(f"role {spec['name']}: {', '.join(sorted(rb))}", "PATCH",
                              f"/guilds/{gid}/roles/{role['id']}", rb))
        for uid in spec.get("members", []):
            member = live.members.get(uid)
            if member and role["id"] not in member["roles"]:
                out.append(Change(f"give role {spec['name']} to {uid}", "PUT",
                                  f"/guilds/{gid}/members/{uid}/roles/{role['id']}"))
    return out


def _plan_channel(cfg: dict, live: Live, cat: dict, spec: dict, prune: bool) -> list[Change]:
    gid = cfg["guild"]["id"]
    ch = live.channel(spec)
    if not ch:
        return []
    out = []
    cb: dict = {}
    if ch["name"] != spec["name"]:
        cb["name"] = spec["name"]
    parent = live.channel(cat, category=True)
    if parent and ch.get("parent_id") != parent["id"]:
        cb["parent_id"] = parent["id"]
    if "topic" in spec and (ch.get("topic") or "") != spec["topic"]:
        cb["topic"] = spec["topic"]
    if "access" in spec and access_of(ch, gid) != spec["access"]:
        others = [o for o in ch.get("permission_overwrites", []) if o["id"] != gid]
        deny = ACCESS_DENY[spec["access"]]
        cb["permission_overwrites"] = others + (
            [{"id": gid, "type": 0, "allow": "0", "deny": str(deny)}] if deny else [])
    if "tags" in spec:
        tags = _tags_body(spec["tags"], ch, prune)
        if _tags_cmp(tags) != _tags_now(ch):
            cb["available_tags"] = tags
    if "default_reaction" in spec:
        now = (ch.get("default_reaction_emoji") or {}).get("emoji_name")
        if now != spec["default_reaction"]:
            cb["default_reaction_emoji"] = {"emoji_name": spec["default_reaction"]}
    if "layout" in spec and ch.get("default_forum_layout") != LAYOUTS[spec["layout"]]:
        cb["default_forum_layout"] = LAYOUTS[spec["layout"]]
    if "sort" in spec and ch.get("default_sort_order") != SORTS[spec["sort"]]:
        cb["default_sort_order"] = SORTS[spec["sort"]]
    if cb:
        out.append(Change(f"#{key(spec)}: {', '.join(sorted(cb))}", "PATCH", f"/channels/{ch['id']}", cb))
    if spec.get("type") == "announcement" and ch["type"] != TYPES["announcement"]:
        out.append(Change(f"#{key(spec)}: type -> announcement", "PATCH", f"/channels/{ch['id']}",
                          {"type": TYPES["announcement"]}))
    return out


def _plan_positions(cfg: dict, live: Live) -> list[Change]:
    order: list[dict] = []
    for cat in cfg.get("category", []):
        c = live.channel(cat, category=True)
        kids = [live.channel(ch) for ch in cat.get("channel", [])]
        if not c or not all(kids):
            return []  # something is still to be created
        order += [c, *kids]
    if not order:
        return []

    def pos(c: dict) -> tuple:
        return (c["position"], int(c["id"]))

    cats_now = [c["id"] for c in sorted((c for c in live.channels if c["type"] == 4), key=pos)]
    cats_want = [c["id"] for c in order if c["type"] == 4]
    same = [i for i in cats_now if i in cats_want] == cats_want
    for cat in (c for c in order if c["type"] == 4):
        kids_now = [c["id"] for c in sorted((c for c in live.channels if c.get("parent_id") == cat["id"]), key=pos)]
        kids_want = [c["id"] for c in order if c.get("parent_id") == cat["id"]]
        same = same and [i for i in kids_now if i in kids_want] == kids_want
    if same:
        return []
    return [Change("reorder channels", "PATCH", f"/guilds/{cfg['guild']['id']}/channels",
                   [{"id": c["id"], "position": i} for i, c in enumerate(order)])]


def plan_posts(cfg: dict, live: Live, refs: dict) -> list[Change]:
    """[[message]] specs of a channel = the bot's posts there, in this order."""
    out: list[Change] = []
    by_channel: dict[str, list[dict]] = {}
    for spec in cfg.get("message", []):
        by_channel.setdefault(spec["channel"], []).append(spec)
    for k, specs in by_channel.items():
        cid = refs["#"].get(k)
        if not cid or k not in live.posts:
            continue
        planned = {"#": {key(ch) for _, ch in channel_specs(cfg)}, "@": {key(r) for r in cfg.get("role", [])},
                   ":": {e["name"] for e in cfg.get("emoji", [])}}
        want = []
        for s in specs:
            img = cfg["_base"] / s["image"] if s.get("image") else None
            want.append((render(s.get("text", ""), refs, planned), image_name(img) if img else None, img))
        have = [(m.get("content", ""), m["attachments"][0]["filename"] if m.get("attachments") else None)
                for m in live.posts[k]]
        if len(have) == len(want):
            for m, (text, name, img), (now_text, now_name) in zip(live.posts[k], want, have, strict=True):
                if name != now_name:
                    out.append(Change(f"#{k}: new picture {name}", "PATCH", f"/channels/{cid}/messages/{m['id']}",
                                      {"content": text, "attachments": [{"id": 0, "filename": name}] if img else []},
                                      [(name, img.read_bytes())] if img else None))
                elif text != now_text:
                    out.append(Change(f"#{k}: edit post text", "PATCH", f"/channels/{cid}/messages/{m['id']}",
                                      {"content": text}))
            continue
        out += [Change(f"#{k}: delete old post", "DELETE", f"/channels/{cid}/messages/{m['id']}")
                for m in live.posts[k]]
        for text, name, img in want:
            body = {"content": text, "flags": 4, "allowed_mentions": NO_PINGS}
            files = None
            if img:
                body["attachments"] = [{"id": 0, "filename": name}]
                files = [(name, img.read_bytes())]
            out.append(Change(f"#{k}: post {'picture ' + name if img else 'text'}", "POST",
                              f"/channels/{cid}/messages", body, files))
    return out


def _onboarding_view(prompts: list[dict], default_ids: list[str], enabled: bool) -> tuple:
    def opt(o: dict) -> tuple:
        emoji = o.get("emoji_name") or (o.get("emoji") or {}).get("name")
        return (o["title"], o.get("description") or "", emoji, tuple(sorted(o.get("role_ids", []))),
                tuple(sorted(o.get("channel_ids", []))))
    return (bool(enabled), tuple(sorted(default_ids)),
            tuple((p["title"], bool(p["single_select"]), bool(p["required"]), tuple(opt(o) for o in p["options"]))
                  for p in prompts))


def _plan_onboarding(cfg: dict, live: Live, refs: dict) -> list[Change]:
    ob = cfg.get("onboarding")
    if not ob or live.onboarding is None:
        return []
    if any(c not in refs["#"] for c in ob["default_channels"]):
        return []   # a dry run before new channels exist; the real run creates them first
    old = {p["title"]: p for p in live.onboarding.get("prompts", [])}
    prompts = []
    for p in ob.get("prompt", []):
        was = old.get(p["title"], {})
        old_opts = {o["title"]: o["id"] for o in was.get("options", [])}
        options = []
        for o in p["option"]:
            opt = {"id": old_opts.get(o["title"]) or snowflake(), "title": o["title"],
                   "description": o.get("description", ""),
                   "role_ids": [refs["@"][r] for r in o.get("roles", []) if r in refs["@"]],
                   "channel_ids": [refs["#"][c] for c in o.get("channels", []) if c in refs["#"]]}
            if o.get("emoji"):
                opt["emoji_name"] = o["emoji"]
            options.append(opt)
        prompts.append({"id": was.get("id") or snowflake(), "type": 0, "title": p["title"], "options": options,
                        "single_select": p.get("single_select", False), "required": p.get("required", False),
                        "in_onboarding": True})
    body = {"prompts": prompts, "default_channel_ids": [refs["#"][c] for c in ob["default_channels"]],
            "enabled": ob.get("enabled", True), "mode": 0}
    now = _onboarding_view(live.onboarding.get("prompts", []), live.onboarding.get("default_channel_ids", []),
                           live.onboarding.get("enabled", False))
    if _onboarding_view(prompts, body["default_channel_ids"], body["enabled"]) == now:
        return []
    return [Change("onboarding", "PUT", f"/guilds/{cfg['guild']['id']}/onboarding", body)]


def _plan_welcome_screen(cfg: dict, live: Live, refs: dict) -> list[Change]:
    ws = cfg.get("welcome_screen")
    if not ws:
        return []
    chans = [{"channel_id": refs["#"][c["channel"]], "description": c["description"], "emoji_name": c.get("emoji")}
             for c in ws.get("channel", []) if c["channel"] in refs["#"]]
    body = {"enabled": ws.get("enabled", True), "description": ws.get("description", ""), "welcome_channels": chans}
    now = live.welcome or {}
    same = (("WELCOME_SCREEN_ENABLED" in live.guild.get("features", [])) == body["enabled"]
            and (now.get("description") or "") == body["description"]
            and [(c["channel_id"], c["description"], c.get("emoji_name")) for c in now.get("welcome_channels", [])]
            == [(c["channel_id"], c["description"], c["emoji_name"]) for c in chans])
    return [] if same else [Change("welcome screen", "PATCH", f"/guilds/{cfg['guild']['id']}/welcome-screen", body)]


def plan_extras(cfg: dict, live: Live) -> list[Change]:
    """Server things the file does not know. Deleted only with prune."""
    gid = cfg["guild"]["id"]
    known = {id(live.channel(cat, category=True)) for cat in cfg.get("category", [])}
    known |= {id(live.channel(ch)) for _, ch in channel_specs(cfg)}
    out = [Change(f"extra {TYPE_NAMES.get(c['type'], c['type'])} #{c['name']}", "DELETE", f"/channels/{c['id']}")
           for c in live.channels if id(c) not in known]
    roles = {id(live.role(r)) for r in cfg.get("role", [])}
    out += [Change(f"extra role {r['name']}", "DELETE", f"/guilds/{gid}/roles/{r['id']}")
            for r in live.roles if id(r) not in roles and r["id"] != gid and not r.get("managed")]
    emojis = {e["name"] for e in cfg.get("emoji", [])}
    out += [Change(f"extra emoji :{e['name']}:", "DELETE", f"/guilds/{gid}/emojis/{e['id']}")
            for e in live.emojis if e["name"] not in emojis]
    return out


# --- run ----------------------------------------------------------------------------------------

def apply(dc: Discord, cfg: dict, *, dry_run: bool, prune: bool = False, echo=print) -> list[Change]:
    done: list[Change] = []
    live = Live.fetch(dc, cfg)
    for planner in (plan_parents, plan_channels):
        changes = planner(cfg, live)
        _run(dc, changes, dry_run, echo)
        done += changes
        if changes and not dry_run:
            live = Live.fetch(dc, cfg)
    changes = plan_updates(cfg, live, prune)
    _run(dc, changes, dry_run, echo)
    done += changes
    if not dry_run and any(c.desc.startswith("#") and ": new post " in c.desc for c in changes):
        live = Live.fetch(dc, cfg)   # a new forum post exists now: pin it
        changes = plan_forum_posts(cfg, live, live.refs(cfg))
        _run(dc, changes, dry_run, echo)
        done += changes
    extras = plan_extras(cfg, live)
    if prune:
        _run(dc, extras, dry_run, echo)
        done += extras
    else:
        for c in extras:
            echo(f"  ! {c.desc} (not in server.toml; --prune deletes it)")
    if not done:
        echo("  server matches server.toml")
    if any(c.method == "POST" and "/messages" not in c.path for c in done) and not dry_run:
        echo("  new things on the server: run pin-ids to write their ids into server.toml")
    return done


def _run(dc: Discord, changes: list[Change], dry_run: bool, echo) -> None:
    for c in changes:
        echo(("  would " if dry_run else "  ") + c.desc)
        if dry_run:
            continue
        try:
            dc.call(c.method, c.path, c.body, c.files)
        except DiscordError as exc:
            if exc.status != 404 or c.if_missing is None:
                raise
            echo("    404: " + c.if_missing.desc)
            dc.call(c.if_missing.method, c.if_missing.path, c.if_missing.body, c.if_missing.files)


# --- ids ----------------------------------------------------------------------------------------

def pin_ids(dc: Discord, path: Path = CONFIG, echo=print) -> int:
    """Write the live id under every [[role]] / [[category]] / [[category.channel]] that has none.

    Text-level edit: comments and layout of server.toml stay as they are.
    """
    cfg = load(path)
    live = Live.fetch(dc, cfg)
    lines = path.read_text(encoding="utf-8").splitlines()
    out, added, section = [], 0, None
    for i, line in enumerate(lines):
        out.append(line)
        head = re.match(r"\s*\[\[(role|category|category\.channel)\]\]\s*$", line)
        if head:
            section = head.group(1)
            continue
        m = re.match(r'(\s*)name\s*=\s*(".*")\s*$', line)
        if not (section and m):
            continue
        block = itertools.takewhile(lambda s: not s.lstrip().startswith("["), lines[i + 1:])
        if any(re.match(r"\s*id\s*=", s) for s in block):
            section = None
            continue
        name = json.loads(m.group(2))
        spec = {"name": name}
        thing = live.role(spec) if section == "role" else live.channel(spec, category=section == "category")
        if thing:
            out.append(f'{m.group(1)}id = "{thing["id"]}"')
            added += 1
            echo(f"  id for {section} {name}")
        section = None
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return added
