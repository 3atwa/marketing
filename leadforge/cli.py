"""Command line: find leads without the web UI.

    python -m leadforge.cli find --services "booking app for salons" \
        --location "Austin, Texas" --sources osm,web --out leads.csv
    python -m leadforge.cli serve --port 8000
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys

from . import ai, db, pipeline


def cmd_find(args) -> int:
    db.init_db()
    targets = args.targets
    if not targets:
        targets = ", ".join(asyncio.run(ai.suggest_targets(args.services)))
        print(f"Targets: {targets}")
    cid = db.create_campaign(args.name or f"CLI: {targets[:40]}", args.services, targets,
                             "\n".join(args.location), args.sources.split(","), args.limit)
    original = db.append_log

    def log(campaign_id: int, line: str) -> None:
        original(campaign_id, line)
        print(line, flush=True)

    db.append_log = log
    asyncio.run(pipeline.run_campaign(cid))
    leads = db.fetch_all("SELECT * FROM leads WHERE campaign_id = ? ORDER BY score DESC", (cid,))
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "category", "emails", "phones", "website", "address", "city", "country",
                        "socials", "score", "signals"])
            for l in leads:
                w.writerow([l["name"], l["category"], "; ".join(l["emails"]), "; ".join(l["phones"]), l["website"],
                            l["address"], l["city"], l["country"], "; ".join(l["socials"].values()), l["score"],
                            "; ".join(l["signals"])])
        print(f"Wrote {len(leads)} leads to {args.out}")
    else:
        for l in leads[:50]:
            print(f"{l['score']:>3}  {l['name'][:35]:35}  {', '.join(l['emails'][:1]):35}  {', '.join(l['phones'][:1])}")
    print(f"Campaign #{cid} saved. Open the web UI to review and reach out.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("leadforge.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="leadforge")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="Discover and enrich leads")
    f.add_argument("--services", default="", help="What you sell (used to suggest target business types)")
    f.add_argument("--targets", default="", help="Comma separated business types, e.g. 'dentist, gym'")
    f.add_argument("--location", action="append", required=True, help="Repeatable, e.g. --location 'Austin, TX'")
    f.add_argument("--sources", default="osm,web", help="osm,web,google_places,gmaps_scraper")
    f.add_argument("--limit", type=int, default=40, help="Max results per search")
    f.add_argument("--name", default="")
    f.add_argument("--out", default="", help="Write CSV here")
    f.set_defaults(func=cmd_find)

    s = sub.add_parser("serve", help="Run the web dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    if args.cmd == "find" and not (args.targets or args.services):
        p.error("give --targets or --services")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
