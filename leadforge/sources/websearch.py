"""Web metasearch via the open-source `ddgs` library (no API key).

Finds business websites for "<target> in <location>". Directory sites (Yelp,
Facebook, ...) are skipped because they are not the business itself; the
enricher then visits each website to pull emails, phones and socials.
"""
from __future__ import annotations

import asyncio
import re

from ..utils import domain_of, is_directory

# Words in result titles that signal a listicle/directory rather than a business.
LISTICLE = re.compile(r"\b(top|best|\d+\s+best|list of|near me|directory|reviews? of)\b", re.I)


def _clean_title(title: str) -> str:
    # "Smile Dental | Family Dentist in Austin" -> "Smile Dental"
    return re.split(r"\s+[|\-–—:·•]\s+", title or "", maxsplit=1)[0].strip()


def _run(query: str, limit: int) -> list[dict]:
    from ddgs import DDGS  # imported lazily: heavy import

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=limit) or [])


async def search(target: str, location: str, limit: int = 40, log=print) -> list[dict]:
    queries = [f"{target} in {location}", f"{target} {location} contact"]
    seen: set[str] = set()
    leads: list[dict] = []
    for q in queries:
        try:
            results = await asyncio.to_thread(_run, q, limit)
        except Exception as exc:  # ddgs raises on rate limits / no results
            log(f"Web search '{q}' failed: {exc}")
            continue
        for r in results:
            url = r.get("href") or r.get("url") or ""
            title = r.get("title") or ""
            d = domain_of(url)
            if not d or d in seen or is_directory(url) or LISTICLE.search(title):
                continue
            seen.add(d)
            leads.append({
                "name": _clean_title(title) or d,
                "category": target,
                "website": f"https://{d}" if url.startswith("https") else f"http://{d}",
                "city": location.split(",")[0].strip(),
                "source": "web",
            })
            if len(leads) >= limit:
                return leads
    return leads
