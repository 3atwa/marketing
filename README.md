# ⚡ LeadForge

**Self-hosted lead generation and outreach for people who sell apps and automation to businesses.**

Tell LeadForge what you sell and where your customers are. It will:

1. **Pick targets.** It suggests the business types most likely to buy your service (e.g. a *booking app* → dentists, salons, gyms, physios), optionally using Claude.
2. **Find businesses** in any city or country, using free open data (OpenStreetMap) and web metasearch, plus Google Maps / Google Places if you enable them.
3. **Visit each business website** (respecting `robots.txt`) and extract **emails, phone numbers, social profiles (Instagram, Facebook, LinkedIn, WhatsApp…) and address**.
4. **Spot sales signals.** It flags things like *no online booking*, *not mobile-friendly*, *no HTTPS* or *outdated website* that make your pitch relevant.
5. **Score and rank** every lead by how reachable and promising it is.
6. **Help you reach out.** It drafts personalised emails from templates (optionally rewritten by AI for each business), sends them through your own SMTP with daily limits, and offers one-click WhatsApp, call and email-app links. It also tracks every lead through `new → contacted → replied → interested → won`.
7. **Audit every website.** It builds a free one-page report for each business (score /100, what to fix, tools detected) that you can send them with `{{report_link}}`. You can see who opens it.
8. **Follow up automatically.** Multi-step sequences (e.g. day 0 → 3 → 7) stop by themselves when a lead replies, bounces or unsubscribes. Replies and bounces are detected by reading your inbox.

### Sales tools

| Feature | What it does for you |
|---|---|
| **Free website audit report** (`/r/<token>`) | A branded page per lead with 12 checks (HTTPS, mobile, speed, online booking, WhatsApp/chat, analytics, SEO title and description, heading, socials, fresh content, Google business data). A "You could win more bookings" report gets far more replies than a generic pitch. **Parked domains** (no real website) are detected and explained. |
| **Report view tracking** | When a human (not an email scanner bot) opens the report, the lead gets 👀, +15 score, an activity entry and a `report.viewed` webhook. Call them while they're interested. |
| **Tech stack detection** | 40+ tools: WordPress, Wix, Shopify, Squarespace, Webflow, Elementor, Google Analytics/Tag Manager, Meta/TikTok Pixel, Calendly, Fresha, Booksy, OpenTable, Zocdoc, WhatsApp buttons, Intercom, Stripe… Pitch a booking app to sites without one, or an integration to sites that use a tool you support. |
| **Email verification** | Checks every email's domain accepts mail (DNS MX via DNS-over-HTTPS). Dead addresses are flagged ✕ and never emailed, which protects your sender reputation. |
| **Follow-up sequences** | Build sequences from your templates with day delays. A default 3-step sequence (report → follow-up → last check-in) is included. Follow-ups are threaded under your first email. |
| **Reply & bounce detection (IMAP)** | Reads your inbox every 5 minutes. Replies set the lead to *replied*, stop its sequence and save the reply on the lead page. "Remove me / not interested" replies add the address to do-not-contact. Bounces mark the email invalid and suppress it. |
| **Pipeline board** | Drag-and-drop Kanban (`/pipeline`) from *new* to *won*. Moving a lead to replied, interested, won, lost or do-not-contact stops its follow-ups. |
| **Funnel dashboard** | Leads → contacted → viewed report → replied → interested → won, with reply rate, bounces and a live activity feed. |
| **CSV import** | Upload your own lists. Columns are auto-mapped, duplicates merged, and each site is optionally crawled for contacts, tech and audit. |
| **Webhooks** | `lead.replied`, `report.viewed` and `lead.status_changed` are POSTed to `WEBHOOK_URL`. Pipe them to Slack, a CRM, Google Sheets or WhatsApp alerts via Zapier, n8n or Make. |

![Dashboard](docs/1-dashboard.png)
![Campaign view](docs/2-campaign.png)
![Lead view](docs/3-lead.png)
![Pipeline](docs/5-pipeline.png)

<img src="docs/4-report-mobile.png" alt="Audit report a lead sees on their phone" width="320">

---

## Quick start

### Option A: Python (3.10+)

```bash
git clone <this repo> leadforge && cd leadforge
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then edit it
python -m leadforge.cli serve                           # open http://127.0.0.1:8000
```

### Option B: Docker

```bash
cp .env.example .env
docker compose up -d                                    # http://localhost:8000
# with the Google Maps scraper as an extra source:
docker compose --profile gmaps up -d                    # and set GMAPS_SCRAPER_URL=http://gmaps:8080
```

### Command line only

```bash
python -m leadforge.cli find \
  --services "Booking & reminder app with WhatsApp notifications" \
  --location "Austin, Texas" --location "Dubai, UAE" \
  --sources osm,web --limit 40 --out leads.csv
```

If you omit `--targets`, LeadForge picks business types from `--services`.

---

## How to use the dashboard

1. **Campaigns → New lead campaign.** Describe what you sell, then click **✨ Suggest business types** (or type your own), add one location per line and choose sources. Click **🚀 Find leads**.
2. Watch the activity log while it searches and crawls. Leads appear sorted by score.
3. Filter (`has email`, `has phone`, min score, status), then select leads and click **✉ Draft emails** (tick **AI personalise** if you set an Anthropic key).
4. In the **Outbox**, review and edit each draft, then **Queue** it and click **▶ Send queued**. Without SMTP, use **Open in mail app**.
5. Open any lead to message it on **WhatsApp**, **call** it, add notes and update its status.
6. **Export CSV** at any time to import into your CRM or Google Sheets.

---

## Lead sources

| Source | Key needed | What you get | Notes |
|---|---|---|---|
| **OpenStreetMap** (Nominatim + Overpass) | No | Name, address, coordinates, often phone, website and email | Free, legal (ODbL). Best coverage in Europe and big cities. |
| **Web search** ([`ddgs`](https://pypi.org/project/ddgs/) metasearch) | No | Business websites | Directories such as Yelp and Facebook are skipped, and the website crawler fills in the contacts. |
| **Google Places API** | `GOOGLE_PLACES_API_KEY` | Name, phone, website, rating, reviews | Official API, with a free monthly credit, then paid. |
| **Google Maps scraper** ([gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper), MIT) | `GMAPS_SCRAPER_URL` | The richest data, including emails | Runs as a separate container. Scraping Google Maps may violate Google's ToS, so use it at your own risk. |

Every lead goes through the **website enricher** (`leadforge/enrich.py`). It reads the homepage and up to 4 contact/about/impressum pages, then extracts:

- **emails**: from `mailto:` links, page text, `name [at] domain [dot] com` obfuscation and schema.org JSON-LD. Emails on the business's own domain are ranked first.
- **phones**: from `tel:` links, JSON-LD and page text, validated and formatted with Google's `libphonenumber`.
- **socials**: Facebook, Instagram, LinkedIn, X, TikTok, YouTube and WhatsApp.
- **sales signals**: no online booking, no mobile app, no live chat, no HTTPS, not mobile-friendly, outdated copyright, sells online.

Duplicates across sources are merged by website domain (or by name + street).

## Configuration

Everything is configured in `.env` (see [`.env.example`](.env.example)):

| Variable | Purpose |
|---|---|
| `APP_PASSWORD` | Password-protects the dashboard. **Set this if you deploy it publicly.** |
| `PUBLIC_BASE_URL`, `SECRET_KEY` | Used for the signed one-click unsubscribe links in emails |
| `SMTP_*`, `FROM_EMAIL`, `FROM_NAME`, `SENDER_COMPANY` | Your mailbox for sending (Gmail/Google Workspace app password, Outlook, Zoho, Mailgun, SES, Brevo…) |
| `SENDER_POSTAL_ADDRESS` | Added to every email (required by CAN-SPAM and others) |
| `DAILY_SEND_LIMIT`, `SEND_DELAY_SECONDS` | Keep volume low to protect your domain reputation (start with 20–50/day) |
| `ANTHROPIC_API_KEY` | Enables AI target suggestions and per-lead email personalisation |
| `GOOGLE_PLACES_API_KEY`, `GMAPS_SCRAPER_URL` | Extra lead sources |
| `AUTOMATION_ENABLED`, `AUTO_SEND` | Background scheduler (every minute) for sequences and sending queued email |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD` | Your inbox, for reply and bounce detection (user and password default to the SMTP ones) |
| `CALENDAR_URL`, `SENDER_WHATSAPP` | "Book a call" and "Chat on WhatsApp" buttons on audit reports; `{{calendar_url}}` in templates |
| `WEBHOOK_URL` | Receives JSON events for Zapier, n8n, Make, Slack… |

## Email templates

Templates support these placeholders: `{{business_name}}`, `{{category}}`, `{{category_plural}}`, `{{city}}`, `{{website}}`, `{{rating}}`, `{{services}}`, `{{my_name}}`, `{{my_company}}`, `{{report_link}}` (the lead's free audit report), `{{calendar_url}}` and `{{signal_line}}`. `{{signal_line}}` is a sentence based on what was found on the lead's website, e.g. *"I noticed customers can't book or order online on your website yet."* Jinja expressions work too.

## JSON API

- `GET /api/campaigns`
- `GET /api/campaigns/{id}/leads?has_email=1&min_score=50`

Use these to push leads into n8n, Zapier, Make or your CRM.

## Responsible use (please read)

Cold outreach is legal in many places **if you do it right**, and the app is built to make that the default:

- Every email sent via SMTP includes your identity, postal address and a **one-click unsubscribe link** (+ `List-Unsubscribe` headers). Unsubscribes are permanent and checked before every send.
- The crawler identifies itself, **respects `robots.txt`** and only reads public business pages.
- WhatsApp is **click-to-chat only** (you send each message yourself). Bulk automated WhatsApp messages to people who haven't opted in violate WhatsApp's terms and get numbers banned.
- **Your obligations:** follow the rules where you and your leads are, e.g. CAN-SPAM (US), GDPR/PECR (EU/UK; B2B outreach usually relies on *legitimate interest*, so keep it relevant, honest and easy to opt out of), CASL (Canada), and the UAE/Saudi telecom and data rules. Contact businesses, not private individuals, and keep volumes modest.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

Project layout:

```
leadforge/
  main.py          FastAPI app + routes
  pipeline.py      campaign runner (discover → dedupe → enrich → score)
  sources/         osm.py, websearch.py, google_places.py, gmaps_scraper.py
  enrich.py        website crawler + email/phone/social extraction + signals + scoring
  outreach.py      templates, SMTP sending, unsubscribe, WhatsApp/tel/mailto links
  ai.py            optional Claude integration
  db.py            SQLite schema and helpers
  cli.py           command line
  templates/       Jinja HTML templates
```

## License

MIT
