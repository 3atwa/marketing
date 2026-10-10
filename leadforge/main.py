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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, automation, db, outreach, pipeline
from .config import settings
from .sources import available_sources
from .utils import pluralize

BASE = Path(__file__).resolve().parent
LEAD_STATUSES = ["new", "contacted", "replied", "interested", "won", "lost", "do_not_contact"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    # Campaigns interrupted by a restart are marked failed so they can be re-run.
    db.execute("UPDATE campaigns SET status='failed', progress='Interrupted by restart' WHERE status='running'")
    task = asyncio.create_task(automation.scheduler_loop()) if settings.automation_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="LeadForge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2)
templates.env.filters["plural"] = pluralize
templates.env.globals.update(
    whatsapp_link=outreach.whatsapp_link, tel_link=outreach.tel_link, mailto_link=outreach.mailto_link,
    settings=settings, LEAD_STATUSES=LEAD_STATUSES,
)

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
PUBLIC_PATHS = ("/unsubscribe", "/static", "/healthz", "/r/")


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
        "form": {}, "suggested": [], "funnel": funnel_stats(), "activity": recent_activity(),
    })


def funnel_stats() -> dict:
    row = db.fetch_one(
        "SELECT COUNT(*) AS leads, "
        "SUM(status != 'new') AS contacted, "
        "SUM(report_views > 0) AS viewed, "
        "SUM(status IN ('replied','interested','won')) AS replied, "
        "SUM(status IN ('interested','won')) AS interested, "
        "SUM(status = 'won') AS won FROM leads") or {}
    sent = db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE channel='email' AND status='sent'")["n"]
    received = db.fetch_one("SELECT COUNT(DISTINCT lead_id) AS n FROM messages WHERE status='received'")["n"]
    bounced = db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE error='bounced'")["n"]
    active = db.fetch_one("SELECT COUNT(*) AS n FROM enrollments WHERE status='active'")["n"]
    stats = {k: (row.get(k) or 0) for k in ("leads", "contacted", "viewed", "replied", "interested", "won")}
    stats.update(sent=sent, received=received, bounced=bounced, active_sequences=active,
                 reply_rate=round(100 * received / sent) if sent else 0)
    return stats


def recent_activity(limit: int = 12) -> list[dict]:
    return db.fetch_all(
        "SELECT e.*, l.name AS lead_name FROM events e LEFT JOIN leads l ON l.id = e.lead_id "
        "WHERE e.kind IN ('report_view','reply','bounce','status','email_sent') ORDER BY e.id DESC LIMIT ?", (limit,))


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
        "seqs": db.fetch_all("SELECT id, name FROM sequences ORDER BY id"),
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
    if action == "enroll":
        res = automation.enroll(ids, int(form.get("sequence_id") or 0))
        return redirect(f"/sequences?flash=Enrolled+{res['enrolled']}+leads+({res['skipped']}+skipped:+no+email,"
                        f"+unsubscribed+or+already+enrolled)")
    if action == "enrich":
        asyncio.get_running_loop().create_task(pipeline.re_enrich(ids))
    elif action in LEAD_STATUSES:
        for i in ids:
            automation.set_status(i, action, "bulk update")
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
        "report_link": ctx["report_link"],
        "events": db.fetch_all("SELECT * FROM events WHERE lead_id = ? ORDER BY id DESC LIMIT 30", (lid,)),
        "enrollments": db.fetch_all("SELECT e.*, s.name AS seq_name FROM enrollments e JOIN sequences s "
                                    "ON s.id = e.sequence_id WHERE e.lead_id = ? ORDER BY e.id DESC", (lid,)),
        "seqs": db.fetch_all("SELECT id, name FROM sequences ORDER BY id"),
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
        "UPDATE leads SET name=?, website=?, emails=?, phones=?, address=?, notes=? WHERE id=?",
        (form.get("name", lead["name"]), form.get("website", lead["website"]),
         json.dumps(split(form.get("emails", ""))), json.dumps(split(form.get("phones", ""))),
         form.get("address", lead["address"]), form.get("notes", ""), lid),
    )
    automation.set_status(lid, status, "edited")
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
    db.log_event(lid, f"{channel}_logged", body[:200])
    return redirect(f"/leads/{lid}")


@app.post("/leads/{lid}/status")
async def lead_status(request: Request, lid: int):
    """Used by the pipeline board's drag and drop."""
    data = await request.json()
    if data.get("status") not in LEAD_STATUSES:
        raise HTTPException(400, "bad status")
    automation.set_status(lid, data["status"], "pipeline board")
    return {"ok": True}


@app.post("/leads/{lid}/enroll")
def lead_enroll(lid: int, sequence_id: int = Form(...)):
    automation.enroll([lid], sequence_id)
    return redirect(f"/leads/{lid}")


@app.post("/leads/{lid}/stop-sequences")
def lead_stop_sequences(lid: int):
    automation.stop_sequences(lid, "stopped manually")
    return redirect(f"/leads/{lid}")


# ------------------------------------------------------------------ public audit report

@app.get("/r/{token}", response_class=HTMLResponse)
def audit_report(request: Request, token: str):
    lead = db.fetch_one("SELECT * FROM leads WHERE report_token = ?", (token,))
    if not lead:
        raise HTTPException(404)
    camp = db.fetch_one("SELECT services FROM campaigns WHERE id = ?", (lead["campaign_id"],))
    if request.query_params.get("preview") != "1":
        automation.record_report_view(lead, request.headers.get("user-agent", ""))
    audit = lead["audit"] or {}
    checks = audit.get("checks", [])
    wa_text = f"Hi, I saw the website report for {lead['name']} and I'd like to know more."
    return templates.TemplateResponse(request, "report.html", {
        "lead": lead, "audit": audit, "checks": checks, "fails": [c for c in checks if not c["ok"]],
        "services": camp["services"] if camp else "",
        "wa_link": outreach.whatsapp_link(settings.sender_whatsapp, wa_text) if settings.sender_whatsapp else "",
    })


# ------------------------------------------------------------------ pipeline board

@app.get("/pipeline", response_class=HTMLResponse)
def pipeline_board(request: Request, campaign: int = 0):
    where, args = ("WHERE campaign_id = ?", [campaign]) if campaign else ("", [])
    leads = db.fetch_all(f"SELECT id, name, category, city, score, status, report_views, emails, phones FROM leads "
                         f"{where} ORDER BY score DESC LIMIT 2000", args)
    columns = {s: [l for l in leads if l["status"] == s] for s in LEAD_STATUSES}
    return templates.TemplateResponse(request, "pipeline.html", {
        "columns": columns, "campaign": campaign,
        "campaigns": db.fetch_all("SELECT id, name FROM campaigns ORDER BY id DESC"),
    })


# ------------------------------------------------------------------ sequences / automation

@app.get("/sequences", response_class=HTMLResponse)
def sequences_page(request: Request):
    seqs = db.fetch_all("SELECT * FROM sequences ORDER BY id")
    counts = {(r["sequence_id"], r["status"]): r["n"] for r in db.fetch_all(
        "SELECT sequence_id, status, COUNT(*) AS n FROM enrollments GROUP BY sequence_id, status")}
    for sq in seqs:
        sq["counts"] = {st: counts.get((sq["id"], st), 0) for st in ("active", "done", "stopped")}
    return templates.TemplateResponse(request, "sequences.html", {
        "seqs": seqs, "tpls": db.fetch_all("SELECT id, name FROM templates ORDER BY id"),
        "flash": request.query_params.get("flash", ""),
        "due": db.fetch_one("SELECT COUNT(*) AS n FROM enrollments WHERE status='active' AND next_at <= ?",
                            (automation._utc(),))["n"],
    })


@app.post("/sequences")
async def sequence_save(request: Request):
    form = await request.form()
    sid = int(form.get("sid") or 0)
    if form.get("action") == "delete" and sid:
        db.execute("DELETE FROM sequences WHERE id = ?", (sid,))
        return redirect("/sequences")
    steps = []
    for tid, delay in zip(form.getlist("template_id"), form.getlist("delay_days")):
        if str(tid).isdigit():
            steps.append({"template_id": int(tid), "delay_days": max(0, int(delay or 0))})
    name = str(form.get("name") or "Sequence").strip()
    if sid:
        db.execute("UPDATE sequences SET name=?, steps=? WHERE id=?", (name, json.dumps(steps), sid))
    elif steps:
        db.execute("INSERT INTO sequences (name, steps, created_at) VALUES (?, ?, ?)", (name, json.dumps(steps), db.now()))
    return redirect("/sequences")


@app.post("/automation/run")
async def automation_run():
    res = await automation.tick()
    msg = f"Automation ran: {res['queued']} follow-ups queued, {res['sent']} sent, {res['replies']} replies, {res['bounces']} bounces"
    return redirect("/sequences?flash=" + msg.replace(" ", "+"))


# ------------------------------------------------------------------ import

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return templates.TemplateResponse(request, "import.html", {
        "campaigns": db.fetch_all("SELECT id, name FROM campaigns ORDER BY id DESC"),
    })


@app.post("/import")
async def import_csv(file: UploadFile = File(...), campaign_id: int = Form(0), name: str = Form(""),
                     services: str = Form(""), enrich: str = Form("")):
    raw = (await file.read()).decode("utf-8-sig", "replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
    if not rows:
        raise HTTPException(400, "The file has no rows")
    if not campaign_id:
        campaign_id = db.create_campaign(name or f"Import: {file.filename}", services, "imported", "", ["import"], 0)
        db.update_campaign(campaign_id, status="done", progress=f"Imported {len(rows)} rows")
    count = pipeline.import_rows(campaign_id, rows)
    db.append_log(campaign_id, f"Imported {count} leads from {file.filename}")
    if enrich:
        asyncio.get_running_loop().create_task(pipeline.enrich_campaign(campaign_id))
    return redirect(f"/campaigns/{campaign_id}")


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

@app.post("/settings/test-webhook")
def test_webhook():
    automation.fire_webhook("test", {"message": "LeadForge webhook works"})
    return redirect("/settings")


@app.get("/api/campaigns")
def api_campaigns():
    return db.fetch_all("SELECT * FROM campaigns ORDER BY id DESC")


@app.get("/api/campaigns/{cid}/leads")
def api_leads(request: Request, cid: int):
    where, args = _lead_filters(cid, dict(request.query_params))
    return JSONResponse(db.fetch_all(f"SELECT * FROM leads WHERE {where} ORDER BY score DESC", args))
