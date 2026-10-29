"""python -m ops.discord <command>. See ops/discord/README.md."""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from . import community, inbox, issues, releases, server
from .api import Discord


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ops.discord")
    p.add_argument("--config", type=Path, default=server.CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pin-ids", help="write live ids of new roles/categories/channels into server.toml")

    s = sub.add_parser("apply", help="server.toml -> live server")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--prune", action="store_true", help="also delete what server.toml does not list")

    s = sub.add_parser("announce", help="post a release to #releases (-rc. tags: #mod-log)")
    s.add_argument("--tag", required=True)
    s.add_argument("--url", required=True, help="release page")
    s.add_argument("--notes", type=Path, required=True, help="markdown file")
    s.add_argument("--channel", help="override the target channel name")
    s.add_argument("--download", help="direct URL of the Setup.exe asset: a link and a button in the post")

    s = sub.add_parser("fixed", help="#bugs posts tagged fixed-in-next -> fixed, reply with the version")
    s.add_argument("--tag", required=True)
    s.add_argument("--url", required=True)

    s = sub.add_parser("inbox", help="new #bugs/#ideas posts -> .discord_inbox/")
    s.add_argument("--all", action="store_true", help="ignore what was already collected")

    sub.add_parser("sync-issues", help="#bugs <-> GitHub Issues of the public repo; then roles for contributions")
    sub.add_parser("award-roles", help="Bug hunter / Idea maker to the authors of confirmed bugs and planned ideas")
    sub.add_parser("roadmap", help="rebuild the #roadmap post from #ideas (code repo only: private terms list)")

    s = sub.add_parser("poll", help="a Discord poll (code repo only: private terms list)")
    s.add_argument("--channel", default="general")
    s.add_argument("--question", required=True)
    s.add_argument("--answer", action="append", required=True, help="repeat 2-10 times")
    s.add_argument("--hours", type=int, default=72)
    s.add_argument("--multi", action="store_true", help="allow several answers")

    s = sub.add_parser("icon", help="PNG -> server icon and bot avatar")
    s.add_argument("png", type=Path)

    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    dc = Discord()

    if a.cmd == "pin-ids":
        print(f"  {server.pin_ids(dc, a.config)} ids written")
    elif a.cmd == "apply":
        server.apply(dc, server.load(a.config), dry_run=a.dry_run, prune=a.prune)
    elif a.cmd == "announce":
        notes = a.notes.read_text(encoding="utf-8-sig")
        releases.announce(dc, server.load(a.config), a.tag, a.url, notes, a.channel, download=a.download)
    elif a.cmd == "fixed":
        releases.mark_fixed(dc, server.load(a.config), a.tag, a.url)
    elif a.cmd == "inbox":
        inbox.collect(dc, server.load(a.config), everything=a.all)
    elif a.cmd == "sync-issues":
        cfg = server.load(a.config)
        issues.sync(dc, issues.GitHub(), cfg)
        community.award_roles(dc, cfg)   # a status from GitHub may have just confirmed a bug
    elif a.cmd == "award-roles":
        community.award_roles(dc, server.load(a.config))
    elif a.cmd == "roadmap":
        community.roadmap(dc, server.load(a.config))
    elif a.cmd == "poll":
        community.poll(dc, server.load(a.config), a.channel, a.question, a.answer, a.hours, a.multi)
    elif a.cmd == "icon":
        uri = "data:image/png;base64," + base64.b64encode(a.png.read_bytes()).decode()
        dc.patch(f"/guilds/{server.load(a.config)['guild']['id']}", {"icon": uri})
        dc.patch("/users/@me", {"avatar": uri})  # the bot posts the release news: same face as the product
        print("  server icon and bot avatar set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
