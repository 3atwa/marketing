"""LeadForge web app: dashboard, campaigns, leads, outbox, templates."""
from __future__ import annotations

import asyncio
import base64
import csv
import hmac
import io
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, db, outreach, pipeline
from .config import settings
from .sources import available_sources

BASE = Path(__file__).resolve().parent
LEAD_STATUSES = ["new", "contacted", "replied", "interested", "won", "lost", "do_not_contact"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    # Campaigns interrupted by a restart are marked failed so they can be re-run.
    db.execute("UPDATE campaigns SET status='failed', progress='Interrupted by restart' WHERE status='running'")
    yield


app = FastAPI(title="LeadForge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)
templates.env.globals.update(
    whatsapp_link=outreach.whatsapp_link, tel_link=outreach.tel_link, mailto_link=outreach.mailto_link,
    settings=settings, LEAD_STATUSES=LEAD_STATUSES,
)

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PUBLIC_PATHS = ("/unsubscribe", "/static", "/healthz")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Protect the dashboard with HTTP basic auth when APP_PASSWORD is set."""
    if APP_PASSWORD and not request.url.path.startswith(PUBLIC_PATHS):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(pwd, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="LeadForge"'})
    return await call_next(request)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def lead_ids_from(form) -> list[int]:
    return [int(x) for x in form.getlist("lead_ids") if str(x).isdigit()]


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------ dashboard

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    campaigns = db.fetch_all(
        "SELECT c.*, COUNT(l.id) AS lead_count, SUM(l.emails != '[]') AS email_count, "
        "SUM(l.phones != '[]') AS phone_count FROM campaigns c LEFT JOIN leads l ON l.campaign_id = c.id "
        "GROUP BY c.id ORDER BY c.id DESC"
    )
    totals = db.fetch_one(
        "SELECT COUNT(*) AS leads, SUM(emails != '[]') AS emails, SUM(phones != '[]') AS phones, "
        "SUM(status IN ('replied','interested','won')) AS warm FROM leads"
    )
    sent = db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE status='sent'")["n"]
    return templates.TemplateResponse(request, "index.html", {
        "campaigns": campaigns, "totals": totals, "sent": sent, "sources": available_sources(),
        "form": {}, "suggested": [],
    })


@app.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request, services: str = Form(""), name: str = Form(""), targets: str = Form(""),
                  locations: str = Form(""), max_per_query: int = Form(40)):
    suggested = await ai.suggest_targets(services) if services.strip() else []
    form = {"name": name, "services": services, "targets": targets or ", ".join(suggested),
            "locations": locations, "max_per_query": max_per_query}
    campaigns = db.fetch_all("SELECT c.*, 0 AS lead_count, 0 AS email_count, 0 AS phone_count FROM campaigns c ORDER BY id DESC")
    return templates.TemplateResponse(request, "index.html", {
        "campaigns": campaigns, "totals": None, "sent": None, "sources": available_sources(),
        "form": form, "suggested": suggested,
    })


@app.post("/campaigns")
async def create_campaign(request: Request):
    form = await request.form()
    services = str(form.get("services", "")).strip()
    targets = str(form.get("targets", "")).strip()
    if not targets and services:
        targets = ", ".join(await ai.suggest_targets(services))
    locations = str(form.get("locations", "")).strip()
    if not targets or not locations:
        raise HTTPException(400, "Business types and at least one location are required")
    name = str(form.get("name", "")).strip() or f"{targets.split(',')[0].strip().title()} - {locations.splitlines()[0]}"
    sources = [s for s in form.getlist("sources")] or ["osm", "web"]
    max_per_query = max(5, min(200, int(form.get("max_per_query") or 40)))
    cid = db.create_campaign(name, services, targets, locations, sources, max_per_query)
    pipeline.start_campaign(cid)
    return redirect(f"/campaigns/{cid}")


# ------------------------------------------------------------------ campaigns

def _lead_filters(campaign_id: int, params) -> tuple[str, list]:
    where, args = ["campaign_id = ?"], [campaign_id]
    if params.get("has_email"):
        where.append("emails != '[]'")
    if params.get("has_phone"):
        where.append("phones != '[]'")
    if params.get("status"):
        where.append("status = ?")
        args.append(params["status"])
    if params.get("min_score"):
        where.append("score >= ?")
        args.append(int(params["min_score"]))
    if params.get("q"):
        where.append("(name LIKE ? OR emails LIKE ? OR city LIKE ? OR website LIKE ?)")
        args += [f"%{params['q']}%"] * 4
    return " AND ".join(where), args


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_detail(request: Request, cid: int):
    camp = db.fetch_one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    if not camp:
        raise HTTPException(404)
    params = dict(request.query_params)
    where, args = _lead_filters(cid, params)
    leads = db.fetch_all(f"SELECT * FROM leads WHERE {where} ORDER BY score DESC, id", args)
    return templates.TemplateResponse(request, "campaign.html", {
        "camp": camp, "leads": leads, "params": params,
        "tpls": db.fetch_all("SELECT id, name FROM templates ORDER BY id"),
    })


@app.get("/campaigns/{cid}/status")
def campaign_status(cid: int):
    camp = db.fetch_one("SELECT status, progress FROM campaigns WHERE id = ?", (cid,))
    count = db.fetch_one("SELECT COUNT(*) AS n FROM leads WHERE campaign_id = ?", (cid,))["n"]
    return {**(camp or {}), "leads": count}


@app.post("/campaigns/{cid}/run")
async def campaign_run(cid: int):
    pipeline.start_campaign(cid)
    return redirect(f"/campaigns/{cid}")


@app.post("/campaigns/{cid}/delete")
def campaign_delete(cid: int):
    db.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
    return redirect("/")


@app.get("/campaigns/{cid}/export.csv")
def campaign_export(request: Request, cid: int):
    where, args = _lead_filters(cid, dict(request.query_params))
    leads = db.fetch_all(f"SELECT * FROM leads WHERE {where} ORDER BY score DESC", args)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["name", "category", "email", "all_emails", "phone", "all_phones", "website", "address", "city",
            "country", "lat", "lon", "rating", "review_count", "facebook", "instagram", "linkedin", "whatsapp",
            "score", "signals", "status", "source", "notes"]
    w.writerow(cols)
    for l in leads:
        s = l["socials"] or {}
        w.writerow([
            l["name"], l["category"], (l["emails"] or [""])[0], "; ".join(l["emails"]), (l["phones"] or [""])[0],
            "; ".join(l["phones"]), l["website"], l["address"], l["city"], l["country"], l["lat"], l["lon"],
            l["rating"], l["review_count"], s.get("facebook", ""), s.get("instagram", ""), s.get("linkedin", ""),
            s.get("whatsapp", ""), l["score"], "; ".join(l["signals"]), l["status"], l["source"], l["notes"],
        ])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="leads-{cid}.csv"'})


@app.post("/campaigns/{cid}/bulk")
async def campaign_bulk(request: Request, cid: int):
    form = await request.form()
    ids = lead_ids_from(form)
    action = form.get("bulk_action") or form.get("action")
    if not ids:
        return redirect(f"/campaigns/{cid}")
    if action == "draft":
        camp = db.fetch_one("SELECT services FROM campaigns WHERE id = ?", (cid,))
        await outreach.create_drafts(ids, int(form.get("template_id") or 0), camp["services"], bool(form.get("use_ai")))
        return redirect("/outbox")
    if action == "enrich":
        asyncio.get_running_loop().create_task(pipeline.re_enrich(ids))
    elif action in LEAD_STATUSES:
        for i in ids:
            db.execute("UPDATE leads SET status = ? WHERE id = ? AND campaign_id = ?", (action, i, cid))
    elif action == "delete":
        for i in ids:
            db.execute("DELETE FROM leads WHERE id = ? AND campaign_id = ?", (i, cid))
    return redirect(f"/campaigns/{cid}")


# ------------------------------------------------------------------ leads

@app.get("/leads/{lid}", response_class=HTMLResponse)
def lead_detail(request: Request, lid: int):
    lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (lid,))
    if not lead:
        raise HTTPException(404)
    camp = db.fetch_one("SELECT * FROM campaigns WHERE id = ?", (lead["campaign_id"],))
    ctx = outreach.context_for(lead, camp["services"])
    tpl = db.fetch_one("SELECT * FROM templates ORDER BY id LIMIT 1")
    subject = outreach.render(tpl["subject"], ctx) if tpl else ""
    body = outreach.render(tpl["body"], ctx) if tpl else ""
    msgs = db.fetch_all("SELECT * FROM messages WHERE lead_id = ? ORDER BY id DESC", (lid,))
    return templates.TemplateResponse(request, "lead.html", {
        "lead": lead, "camp": camp, "subject": subject, "body": body, "msgs": msgs,
    })


@app.post("/leads/{lid}")
async def lead_update(request: Request, lid: int):
    form = await request.form()
    lead = db.fetch_one("SELECT * FROM leads WHERE id = ?", (lid,))
    if not lead:
        raise HTTPException(404)
    split = lambda s: [x.strip() for x in str(s).replace("\n", ",").split(",") if x.strip()]
    status = form.get("status") if form.get("status") in LEAD_STATUSES else lead["status"]
    db.execute(
        "UPDATE leads SET name=?, website=?, emails=?, phones=?, address=?, status=?, notes=? WHERE id=?",
        (form.get("name", lead["name"]), form.get("website", lead["website"]),
         json.dumps(split(form.get("emails", ""))), json.dumps(split(form.get("phones", ""))),
         form.get("address", lead["address"]), status, form.get("notes", ""), lid),
    )
    if status == "do_not_contact":
        for e in split(form.get("emails", "")):
            db.suppress(e, "marked do-not-contact")
    return redirect(f"/leads/{lid}")


@app.post("/leads/{lid}/log")
async def lead_log_contact(lid: int, channel: str = Form(...), body: str = Form("")):
    """Record a manual WhatsApp message or phone call."""
    db.execute("INSERT INTO messages (lead_id, channel, body, status, created_at, sent_at) VALUES (?, ?, ?, 'sent', ?, ?)",
               (lid, channel, body, db.now(), db.now()))
    db.execute("UPDATE leads SET status='contacted' WHERE id=? AND status='new'", (lid,))
    return redirect(f"/leads/{lid}")


# ------------------------------------------------------------------ outbox

@app.get("/outbox", response_class=HTMLResponse)
def outbox(request: Request, status: str = ""):
    sql = ("SELECT m.*, l.name AS lead_name, l.campaign_id FROM messages m JOIN leads l ON l.id = m.lead_id "
           "WHERE m.channel = 'email'")
    args: list = []
    if status:
        sql += " AND m.status = ?"
        args.append(status)
    msgs = db.fetch_all(sql + " ORDER BY m.id DESC LIMIT 500", args)
    counts = {r["status"]: r["n"] for r in db.fetch_all(
        "SELECT status, COUNT(*) AS n FROM messages WHERE channel='email' GROUP BY status")}
    return templates.TemplateResponse(request, "outbox.html", {
        "msgs": msgs, "counts": counts, "status": status, "sent_today": outreach.sent_today(),
        "flash": request.query_params.get("flash", ""),
    })


@app.post("/messages/{mid}")
async def message_update(mid: int, subject: str = Form(...), body: str = Form(...), action: str = Form("save")):
    db.execute("UPDATE messages SET subject=?, body=? WHERE id=? AND status IN ('draft','failed')", (subject, body, mid))
    if action == "queue":
        db.execute("UPDATE messages SET status='queued', error='' WHERE id=? AND status IN ('draft','failed')", (mid,))
    elif action == "delete":
        db.execute("DELETE FROM messages WHERE id=? AND status != 'sent'", (mid,))
    return redirect("/outbox")


@app.post("/outbox/queue-all")
def queue_all():
    db.execute("UPDATE messages SET status='queued' WHERE channel='email' AND status='draft'")
    return redirect("/outbox?status=queued")


@app.post("/outbox/send")
async def outbox_send():
    if not settings.smtp_configured:
        return redirect("/outbox?flash=SMTP+is+not+configured.+Set+SMTP_*+and+FROM_EMAIL+in+.env,+or+use+the+mailto+buttons.")
    asyncio.get_running_loop().create_task(outreach.send_queued())
    return redirect("/outbox?flash=Sending+in+the+background+(respecting+daily+limit+and+delay).")


# ------------------------------------------------------------------ templates

@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    return templates.TemplateResponse(request, "templates.html", {
        "tpls": db.fetch_all("SELECT * FROM templates ORDER BY id"),
    })


@app.post("/templates")
async def template_save(tid: int = Form(0), name: str = Form(...), subject: str = Form(...), body: str = Form(...),
                        action: str = Form("save")):
    if action == "delete" and tid:
        db.execute("DELETE FROM templates WHERE id = ?", (tid,))
    elif tid:
        db.execute("UPDATE templates SET name=?, subject=?, body=? WHERE id=?", (name, subject, body, tid))
    else:
        db.execute("INSERT INTO templates (name, subject, body, created_at) VALUES (?, ?, ?, ?)",
                   (name, subject, body, db.now()))
    return redirect("/templates")


# ------------------------------------------------------------------ settings / unsubscribe

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "sources": available_sources(),
        "suppressed": db.fetch_all("SELECT * FROM suppression ORDER BY created_at DESC LIMIT 200"),
    })


@app.post("/suppression")
def suppression_add(email: str = Form(...)):
    db.suppress(email.strip(), "added manually")
    return redirect("/settings")


@app.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(request: Request, e: str = "", t: str = ""):
    valid = bool(e) and hmac.compare_digest(t, outreach.unsubscribe_token(e))
    return templates.TemplateResponse(request, "unsubscribe.html", {"email": e, "token": t, "valid": valid, "done": False})


@app.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(request: Request):
    form = await request.form()
    e = str(form.get("e") or request.query_params.get("e", ""))
    t = str(form.get("t") or request.query_params.get("t", ""))
    valid = bool(e) and hmac.compare_digest(t, outreach.unsubscribe_token(e))
    if valid:
        db.suppress(e, "unsubscribed via link")
        db.execute("UPDATE leads SET status='do_not_contact' WHERE emails LIKE ?", (f'%"{e.lower()}"%',))
    return templates.TemplateResponse(request, "unsubscribe.html", {"email": e, "valid": valid, "done": valid})


# ------------------------------------------------------------------ JSON API

@app.get("/api/campaigns")
def api_campaigns():
    return db.fetch_all("SELECT * FROM campaigns ORDER BY id DESC")


@app.get("/api/campaigns/{cid}/leads")
def api_leads(request: Request, cid: int):
    where, args = _lead_filters(cid, dict(request.query_params))
    return JSONResponse(db.fetch_all(f"SELECT * FROM leads WHERE {where} ORDER BY score DESC", args))
