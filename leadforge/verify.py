"""Email deliverability check: does the email's domain accept mail?

Uses DNS-over-HTTPS (works behind proxies and firewalls, no extra
dependency). A domain with MX records -> "valid"; one that doesn't exist or
has neither MX nor A records -> "invalid"; lookup failures -> "unknown".
Sending to invalid addresses causes bounces that hurt your sender reputation.
"""
from __future__ import annotations

import httpx

from .utils import http_client

DOH_URL = "https://cloudflare-dns.com/dns-query"
_cache: dict[str, str] = {}


async def _query(client: httpx.AsyncClient, name: str, rtype: str) -> dict:
    r = await client.get(DOH_URL, params={"name": name, "type": rtype}, headers={"Accept": "application/dns-json"},
                         timeout=10)
    r.raise_for_status()
    return r.json()


async def domain_status(domain: str) -> str:
    domain = domain.lower().strip(".")
    if domain in _cache:
        return _cache[domain]
    try:
        async with http_client() as client:
            mx = await _query(client, domain, "MX")
            if mx.get("Status") == 3:  # NXDOMAIN
                status = "invalid"
            elif any(a.get("type") == 15 for a in mx.get("Answer", [])):
                status = "valid"
            else:
                a = await _query(client, domain, "A")  # RFC 5321: fall back to the A record
                status = "valid" if any(x.get("type") == 1 for x in a.get("Answer", [])) else "invalid"
    except (httpx.HTTPError, ValueError):
        status = "unknown"
    if status != "unknown":
        _cache[domain] = status
    return status


async def email_status(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return await domain_status(email.rsplit("@", 1)[1])
