"""Optional Claude integration (set ANTHROPIC_API_KEY to enable).

* suggest_targets: turn "what I sell" into the business types most likely to buy it.
* personalize: write a short, specific first-touch email for one lead.

Without a key, LeadForge falls back to keyword rules and plain templates.
"""
from __future__ import annotations

import asyncio
import json
import re

from .config import settings

# Fallback mapping used when no API key is configured.
KEYWORD_TARGETS = {
    r"book|appointment|schedul|reservation": ["hair salon", "barber", "beauty salon", "dentist", "physiotherapist",
                                               "spa", "massage", "yoga studio", "gym", "veterinarian"],
    r"restaurant|food|menu|order|delivery|qr": ["restaurant", "cafe", "bakery", "bar", "fast food"],
    r"clinic|patient|medical|health": ["dentist", "clinic", "doctor", "physiotherapist", "pharmacy", "optician"],
    r"real estate|property|listing|rent": ["real estate agency"],
    r"gym|fitness|membership|workout": ["gym", "yoga studio"],
    r"shop|store|e-?commerce|inventory|retail|pos": ["clothing store", "boutique", "florist", "jewelry", "pet store",
                                                    "furniture store", "electronics store"],
    r"hotel|guest|travel|tour": ["hotel", "guest house", "travel agency"],
    r"school|course|student|education|learn": ["language school", "driving school", "kindergarten"],
    r"law|legal|account|invoice|crm|client portal": ["law firm", "accountant", "insurance", "architect"],
    r"car|auto|vehicle|garage": ["car repair", "car dealer", "car wash"],
    r"plumb|electric|contractor|field service|repair": ["plumber", "electrician", "carpenter"],
    r"website|seo|marketing|social media|chatbot|whatsapp|automation|ai": [
        "restaurant", "dentist", "hair salon", "real estate agency", "gym", "law firm", "car repair"],
}


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _ask_json(prompt: str, schema: dict, effort: str = "low") -> dict | None:
    client = _client()
    response = client.beta.messages.create(
        model=settings.anthropic_model,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def keyword_targets(services: str) -> list[str]:
    out: list[str] = []
    for pattern, targets in KEYWORD_TARGETS.items():
        if re.search(pattern, services, re.I):
            out += targets
    return list(dict.fromkeys(out))[:10] or ["restaurant", "dentist", "hair salon", "gym", "real estate agency"]


async def suggest_targets(services: str) -> list[str]:
    if not settings.ai_enabled:
        return keyword_targets(services)
    schema = {
        "type": "object",
        "properties": {"targets": {"type": "array", "items": {"type": "string"}}},
        "required": ["targets"],
        "additionalProperties": False,
    }
    prompt = (
        "I sell the following apps/services to local and small businesses:\n\n"
        f"{services}\n\n"
        "List the 8 types of local businesses most likely to buy this, as short generic search terms "
        "someone would type into Google Maps (e.g. 'dentist', 'hair salon', 'real estate agency'). "
        "Most promising first."
    )
    try:
        data = await asyncio.to_thread(_ask_json, prompt, schema)
    except Exception:
        data = None
    targets = [t.strip().lower() for t in (data or {}).get("targets", []) if t.strip()]
    return targets[:10] or keyword_targets(services)


async def personalize(lead: dict, services: str, sender: str, base_subject: str, base_body: str) -> tuple[str, str] | None:
    """Rewrite a rendered template so it references this specific business."""
    if not settings.ai_enabled:
        return None
    schema = {
        "type": "object",
        "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
        "required": ["subject", "body"],
        "additionalProperties": False,
    }
    facts = {k: lead.get(k) for k in ("name", "category", "city", "website", "rating", "review_count", "signals")}
    prompt = (
        "Rewrite this cold email so it feels written for this one business. Keep it under 120 words, plain text, "
        "friendly and specific, one clear call to action, no hype, no made-up facts, no placeholders. "
        "Mention at most one observation from the facts if it is relevant to what I sell. Keep the sign-off.\n\n"
        f"What I sell: {services}\nSender: {sender}\nBusiness facts (JSON): {json.dumps(facts)}\n\n"
        f"Subject: {base_subject}\n\n{base_body}"
    )
    try:
        data = await asyncio.to_thread(_ask_json, prompt, schema)
    except Exception:
        return None
    if not data or not data.get("body"):
        return None
    return data["subject"].strip(), data["body"].strip()
