"""Server as code: server.toml <-> the live Discord server.

apply only creates and changes. Things on the server that the file does not list are reported;
they are deleted only with prune=True (a human decides that).
"""
from __future__ import annotations

import json
import re
import tomllib
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


# --- config -------------------------------------------------------------------------------------

def load(path: Path = CONFIG) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for msg in cfg.get("message", []):
        if len(msg["text"]) > MESSAGE_LIMIT:
            raise SystemExit(f"message in #{msg['channel']}: {len(msg['text'])} chars, limit {MESSAGE_LIMIT}")
    return cfg


def channel_specs(cfg: dict) -> list[tuple[dict, dict]]:
    """(category spec, channel spec) in the order of the file."""
    return [(cat, ch) for cat in cfg.get("category", []) for ch in cat.get("channel", [])]


def channel_id(cfg: dict, name: str) -> str:
    for _, ch in channel_specs(cfg):
        if ch["name"] == name and ch.get("id"):
            return ch["id"]
    raise SystemExit(f"server.toml: channel #{name} has no id (run export)")


# --- live state ---------------------------------------------------------------------------------

@dataclass
class Live:
    guild: dict
    channels: list[dict]
    roles: list[dict]
    bot_id: str
    members: dict[str, dict] = field(default_factory=dict)
    messages: list[dict | None] = field(default_factory=list)  # same order as cfg["message"]

    @classmethod
    def fetch(cls, dc: Discord, cfg: dict) -> Live:
        gid = cfg["guild"]["id"]
        live = cls(
            guild=dc.get(f"/guilds/{gid}"),
            channels=dc.get(f"/guilds/{gid}/channels"),
            roles=dc.get(f"/guilds/{gid}/roles"),
            bot_id=dc.get("/users/@me")["id"],
        )
        for uid in {u for r in cfg.get("role", []) for u in r.get("members", [])}:
            live.members[uid] = dc.get(f"/guilds/{gid}/members/{uid}")
        for spec in cfg.get("message", []):
            live.messages.append(_find_message(dc, live, cfg, spec))
        return live

    def channel(self, spec: dict, category: bool = False) -> dict | None:
        pool = [c for c in self.channels if (c["type"] == 4) == category]
        by_id = next((c for c in pool if spec.get("id") and c["id"] == spec["id"]), None)
        return by_id or next((c for c in pool if c["name"] == spec["name"]), None)

    def role(self, spec: dict) -> dict | None:
        by_id = next((r for r in self.roles if spec.get("id") and r["id"] == spec["id"]), None)
        return by_id or next((r for r in self.roles if r["name"] == spec["name"]), None)

    def names(self) -> dict[str, str]:
        return {c["name"]: c["id"] for c in self.channels if c["type"] != 4}


def _find_message(dc: Discord, live: Live, cfg: dict, spec: dict) -> dict | None:
    ch = live.channel(_spec_by_name(cfg, spec["channel"]))
    if not ch:
        return None
    if spec.get("id"):
        try:
            return dc.get(f"/channels/{ch['id']}/messages/{spec['id']}")
        except DiscordError as exc:
            if exc.status == 404:
                return None
            raise
    mine = [m for m in dc.get(f"/channels/{ch['id']}/messages?limit=50") if m["author"]["id"] == live.bot_id]
    return mine[-1] if mine else None  # newest first -> the oldest bot message


def _spec_by_name(cfg: dict, name: str) -> dict:
    for _, ch in channel_specs(cfg):
        if ch["name"] == name:
            return ch
    raise SystemExit(f"server.toml: no channel #{name}")


# --- text helpers -------------------------------------------------------------------------------

def render(text: str, names: dict[str, str]) -> str:
    """{#bugs} -> <#id>, so the file stays readable and the message gets clickable links."""
    def sub(m: re.Match) -> str:
        if m.group(1) not in names:
            raise SystemExit(f"unknown channel {{#{m.group(1)}}} in a message")
        return f"<#{names[m.group(1)]}>"
    return re.sub(r"\{#([a-z0-9_-]+)\}", sub, text)


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


# --- plan ---------------------------------------------------------------------------------------

@dataclass
class Change:
    desc: str
    method: str
    path: str
    body: object = None


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
    names = live.names()

    # guild first: Community must be on before a channel can become an announcement channel
    g = cfg.get("guild", {})
    body = {k: g[k] for k in GUILD_FIELDS if k in g and live.guild.get(k) != g[k]}
    for k in GUILD_CHANNELS:
        if k in g:
            want = names.get(g[k])
            if want and live.guild.get(f"{k}_id") != want:
                body[f"{k}_id"] = want
    if g.get("community") and "COMMUNITY" not in live.guild.get("features", []):
        body["features"] = sorted(set(live.guild.get("features", [])) | {"COMMUNITY"})
    if body:
        out.append(Change(f"guild: {', '.join(sorted(body))}", "PATCH", f"/guilds/{gid}", body))

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

    for cat, spec in channel_specs(cfg):
        ch = live.channel(spec)
        if not ch:
            continue
        cb: dict = {}
        if spec.get("id") and ch["name"] != spec["name"]:
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
        if cb:
            out.append(Change(f"#{spec['name']}: {', '.join(sorted(cb))}", "PATCH", f"/channels/{ch['id']}", cb))
        if spec.get("type") == "announcement" and ch["type"] != TYPES["announcement"]:
            out.append(Change(f"#{spec['name']}: type -> announcement", "PATCH", f"/channels/{ch['id']}",
                              {"type": TYPES["announcement"]}))

    out += _plan_positions(cfg, live)

    for spec, msg in zip(cfg.get("message", []), live.messages, strict=False):
        cid = names.get(spec["channel"])
        if not cid:
            continue
        text = render(spec["text"], names)
        if msg is None:
            out.append(Change(f"post message in #{spec['channel']} (then run export to keep its id)", "POST",
                              f"/channels/{cid}/messages", {"content": text, "flags": 4}))
        elif msg["content"] != text:
            out.append(Change(f"edit message in #{spec['channel']}", "PATCH",
                              f"/channels/{cid}/messages/{msg['id']}", {"content": text}))
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

    def key(c: dict) -> tuple:
        return (c["position"], int(c["id"]))

    cats_now = [c["id"] for c in sorted((c for c in live.channels if c["type"] == 4), key=key)]
    cats_want = [c["id"] for c in order if c["type"] == 4]
    same = [i for i in cats_now if i in cats_want] == cats_want
    for cat in (c for c in order if c["type"] == 4):
        kids_now = [c["id"] for c in sorted((c for c in live.channels if c.get("parent_id") == cat["id"]), key=key)]
        kids_want = [c["id"] for c in order if c.get("parent_id") == cat["id"]]
        same = same and [i for i in kids_now if i in kids_want] == kids_want
    if same:
        return []
    return [Change("reorder channels", "PATCH", f"/guilds/{cfg['guild']['id']}/channels",
                   [{"id": c["id"], "position": i} for i, c in enumerate(order)])]


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
    extras = plan_extras(cfg, live)
    if prune:
        _run(dc, extras, dry_run, echo)
        done += extras
    else:
        for c in extras:
            echo(f"  ! {c.desc} (not in server.toml; --prune deletes it)")
    if not done:
        echo("  server matches server.toml")
    return done


def _run(dc: Discord, changes: list[Change], dry_run: bool, echo) -> None:
    for c in changes:
        echo(("  would " if dry_run else "  ") + c.desc)
        if not dry_run:
            res = dc.call(c.method, c.path, c.body)
            if c.method == "POST" and isinstance(res, dict) and res.get("id"):
                echo(f"    id {res['id']}")


# --- export -------------------------------------------------------------------------------------

def _q(s: str) -> str:
    if "\n" in s:
        return '"""\n' + s.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'
    return json.dumps(s, ensure_ascii=False)


def export(dc: Discord, guild_id: str) -> str:
    """Live server -> server.toml text. Messages: bot posts in read-only channels (not announcements)."""
    guild = dc.get(f"/guilds/{guild_id}")
    channels = dc.get(f"/guilds/{guild_id}/channels")
    roles = dc.get(f"/guilds/{guild_id}/roles")
    bot_id = dc.get("/users/@me")["id"]
    ids = {c["id"]: c["name"] for c in channels}

    def key(c: dict) -> tuple:
        return (c["position"], int(c["id"]))

    L = ["# Sessionmate Discord server as code. Apply: python -m ops.discord apply --dry-run, then apply.",
         "# Ids pin renames. {#name} in a message becomes a channel link.", "", "[guild]",
         f"id = {_q(guild_id)}"]
    for k in GUILD_FIELDS:
        if guild.get(k) is not None:
            v = guild[k]
            L.append(f"{k} = {_q(v) if isinstance(v, str) else v}")
    L.append(f"community = {'true' if 'COMMUNITY' in guild.get('features', []) else 'false'}")
    for k in GUILD_CHANNELS:
        if guild.get(f"{k}_id") in ids:
            L.append(f"{k} = {_q(ids[guild[k + '_id']])}")

    for r in sorted(roles, key=lambda r: -r["position"]):
        if r["id"] == guild_id or r.get("managed"):
            continue
        L += ["", "[[role]]", f"name = {_q(r['name'])}", f"id = {_q(r['id'])}",
              f'color = "#{r["color"]:06X}"', f"hoist = {str(r['hoist']).lower()}",
              f"mentionable = {str(r['mentionable']).lower()}",
              "permissions = [" + ", ".join(_q(p) for p in perms_names(int(r["permissions"]))) + "]"]
        holders = [guild["owner_id"]] if r["id"] in dc.get(f"/guilds/{guild_id}/members/{guild['owner_id']}")["roles"] else []
        L.append("members = [" + ", ".join(_q(u) for u in holders) + "]  # owner only; add user ids by hand")

    messages = []
    for cat in sorted((c for c in channels if c["type"] == 4), key=key):
        L += ["", "[[category]]", f"name = {_q(cat['name'])}", f"id = {_q(cat['id'])}"]
        for ch in sorted((c for c in channels if c.get("parent_id") == cat["id"]), key=key):
            L += ["", "[[category.channel]]", f"name = {_q(ch['name'])}", f"id = {_q(ch['id'])}",
                  f"type = {_q(TYPE_NAMES.get(ch['type'], str(ch['type'])))}"]
            access = access_of(ch, guild_id)
            if access != "public":
                L.append(f"access = {_q(access)}")
            if ch.get("topic"):
                L.append(f"topic = {_q(ch['topic'])}")
            if ch["type"] == TYPES["forum"]:
                emoji = (ch.get("default_reaction_emoji") or {}).get("emoji_name")
                if emoji:
                    L.append(f"default_reaction = {_q(emoji)}")
                L.append("tags = [")
                for t in ch.get("available_tags", []):
                    extra = ", moderated = true" if t.get("moderated") else ""
                    extra += f", emoji = {_q(t['emoji_name'])}" if t.get("emoji_name") else ""
                    L.append(f"  {{ name = {_q(t['name'])}{extra} }},")
                L.append("]")
            if access == "read-only" and ch["type"] == TYPES["text"]:
                mine = [m for m in dc.get(f"/channels/{ch['id']}/messages?limit=50") if m["author"]["id"] == bot_id]
                messages += [(ch["name"], m) for m in reversed(mine)]
    for name, m in messages:
        L += ["", "[[message]]", f"channel = {_q(name)}", f"id = {_q(m['id'])}",
              f"text = {_q(unrender(m['content'], ids))}"]
    orphans = [c["name"] for c in channels if c["type"] != 4 and not c.get("parent_id")]
    if orphans:
        L += ["", "# Not exported (no category): " + ", ".join(orphans)]
    return "\n".join(L) + "\n"
