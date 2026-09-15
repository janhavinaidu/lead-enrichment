# ROX Test

A functional Streamlit prototype that verifies the complete ROX lead-generation pipeline end to end **before** the production ROX application is built.

This is **not** the final UI. It is a real integration test using the actual external services:

```
Event Name + Market
        ↓
Groq (targeting instructions)
        ↓
OpenOutreach / OpenOutFind (free lead discovery)
        ↓
Display leads → select up to 4
        ↓
Apify Actor 1 (LinkedIn search / short profiles)
        ↓
Apify Actor 2 (deep profile enrichment + email)
        ↓
Groq (personalized outreach email)
        ↓
Review → Approve  (SMTP NOT connected yet)
```

## 1. What ROX Test does

- Converts an **Event Name + Market** (e.g. `IDA 2026` + `Automobile`) into targeting instructions using **Groq**.
- Uses **OpenOutreach/OpenOutFind** to discover and qualify 10–20 relevant leads (free discovery/qualification stage only — **no paid email resolution**).
- Lets you select up to 4 leads and enrich them via **two Apify Actors** (search/short profile → deep profile + email).
- Generates a **personalized outreach email** for each lead with **Groq**.
- Stores everything in a local **SQLite** database (`rox.db`) for testing/persistence.
- Does **not** send emails. A placeholder "Send Email" button only shows that SMTP will be connected later.

Nothing is faked — if any external call fails, you see the real error.

## 2. Install Python dependencies

```bash
cd testing_mvp
pip install -r requirements.txt
```

## 3. Install OpenOutreach

OpenOutreach ships as a CLI installed from PyPI:

```bash
pip install openoutreach
```

Verify it is available:

```bash
openoutreach status
```

> Note: OpenOutFind (the finder engine) is bundled with `openoutreach`. If you only want the finder, `pip install openoutfind` gives you the `outfind` CLI — `ROX Test` auto-detects both.

OpenOutreach requires `OPENOUTFIND_*` environment variables (see 4 below). It is configured entirely through environment variables, not a config file.

## 4. Configure `.env`

Copy the template and fill in the values:

```bash
cp .env.example .env
```

Required:

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | Groq API key (console.groq.com) |
| `APIFY_API_TOKEN` | Apify API token (console.apify.com/settings/integrations) |
| `BETTERCONTACT_API_KEY` | BetterContact key — **required by OpenOutreach even for the free discovery stage** |
| `CONTACT_COMPASS_API_KEY` | Contact Compass token (contactcompass.io) — used for email finding |
| `APIFY_SEARCH_ACTOR_ID` | Apify Actor 1 ID (LinkedIn search / short profile) |
| `APIFY_PROFILE_ACTOR_ID` | Apify Actor 2 ID (deep profile) |

OpenOutreach configuration (all optional here; provide the ones your campaign needs):

| Variable | Purpose |
|---|---|
| `OPENOUTFIND_PRODUCT_DOCS` | Path to a file describing your product |
| `OPENOUTFIND_CAMPAIGN_TARGET` | Path to a file with campaign targeting (ROX Test writes Groq's targeting here automatically) |
| `OPENOUTFIND_AI_MODEL` | LLM model OpenOutreach should use for qualification |
| `OPENOUTFIND_LLM_API_KEY` | LLM API key OpenOutreach should use |
| `OPENOUTFIND_OPERATOR_EMAIL` | Your operator email |
| `OPENOUTFIND_OPERATOR_COUNTRY` | Your country |
| `OPENOUTFIND_ACCEPT_LEGAL_NOTICE` | `true` to accept OpenOutreach's legal notice |

Follow the up-to-date OpenOutreach README / `openoutreach init` for any additional variables — do not invent them.

## 5. Obtain API keys

- **Groq**: create an account at https://console.groq.com → API Keys → Create API Key.
- **Apify**: create an account at https://console.apify.com → Settings → Integrations → copy your API token.
- **BetterContact**: create an account at https://bettercontact.com (free tier includes discovery access). Paste the key into `BETTERCONTACT_API_KEY`. OpenOutreach uses this for the *free* discovery stage.
- **Contact Compass**: create an account at https://contactcompass.io → get your API token (20,000 free email lookups). Paste it into `CONTACT_COMPASS_API_KEY`. ROX Test passes this token to the Apify profile Actor as `contactCompassToken` so the Actor can find a work email for each LinkedIn profile via Contact Compass.
- **OpenOutreach LLM key**: if `OPENOUTFIND_AI_MODEL` / `OPENOUTFIND_LLM_API_KEY` are unset, OpenOutreach may fall back to its own defaults — set them if the docs for your install require it.

## 6. Configure the two Apify Actor IDs

ROX Test uses two separate Apify Actors. You must create/pick them and put their Actor IDs into `.env`:

- **`APIFY_SEARCH_ACTOR_ID`**: an Actor that reads a LinkedIn (profile/search) URL and returns short profile/search information.
- **`APIFY_PROFILE_ACTOR_ID`**: an Actor that takes a LinkedIn profile URL and returns a deep profile (about, experience, education, skills, location).

**Email finding:** the profile Actor is where emails are found. ROX Test passes `CONTACT_COMPASS_API_KEY` to the profile Actor as the ContactCompass token (`contactCompassToken` / `findContacts.contactCompassToken`) and enables email resolution (`findContacts: true` / `find_email: true` / `resolveEmails: true`). Pick an Actor that supports ContactCompass integration, such as:
- `supreme_coder/linkedin-profile-scraper` — works, but Contact ComPass hit-rate is limited (it only returns an email when ComPass actually has the person on file, which is uncommon for non-US / smaller-company execs). If it comes back with `email: null` it is normal, not a bug.
- `devwithbobby/linkedin-people-scraper` (Recommended) — input `findContacts: true` + `contactCompassToken`; confirms the found email via domain verification. **Required inputs:** `cookie` (array of LinkedIn cookies from the Cookie-Editor extension), `userAgent`, `proxy`.

**Important caveat:** Contact Compass is a *lookup* service, not an unlimited email finder. Coverage is partial — expect email hits well under 100%, and 20k free lookups that run out. The pipeline correctly treats a missing email as a normal "no email found" outcome and never invents one. If you need higher hit-rates, pair the profile Actor with a second, dedicated email-finder Actor (e.g. `iron-crawler/linkedin-email-finder-profile`) instead — out of scope for this MVP.

Choose Actors whose input schema accepts something like `url` / `profileUrls` / `profiles`. R O X Test sends a generic superset input; the Actor ignores keys that aren't in its schema.

The raw output fields are not assumed — ROX Test inspects the actual Actor output at runtime and normalizes it (see `services/apify_service.py`).

## 7. Run the app

```bash
streamlit run app.py
```

Then open the printed local URL (default http://localhost:8501).

### Using the app

1. Enter an Event Name (e.g. `IDA 2026`) and Market (e.g. `Automobile`), pick the number of leads (10/15/20), click **Find Leads**.
2. Review the Groq-generated targeting instructions, then click **Run Lead Search**.
3. Select up to 4 leads → click **Research Selected Leads**.
4. Review the enriched profiles, then click **Generate Personalized Emails**.
5. Review/edit each email → **Regenerate** or **Approve**.
6. The **Send Email** button is a placeholder — SMTP comes later.

## 8. Debug Mode

Enable **Debug Mode** in the sidebar to see:

- The exact Groq targeting prompt
- The OpenOutreach command + stdout + stderr
- Apify Actor IDs and raw Actor outputs
- The Groq email-generation prompts

API keys are never displayed.

## 9. Project structure

```text
testing_mvp/
├── app.py                      # Streamlit UI + pipeline
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── services/
│   ├── groq_service.py         # targeting + email generation
│   ├── openoutreach_service.py # CLI subprocess wrapper (JSONL/CSV)
│   ├── apify_service.py        # Actor 1 + Actor 2 runners, profile normalization
│   └── email_service.py        # placeholder (SMTP later)
├── database/
│   └── db.py                   # SQLite (campaign / lead / enrichment / email)
└── models/
    └── schemas.py              # dataclasses
```

## 10. Important notes

- **OpenOutreach counts are cumulative.** `openoutreach find N` finds *N more* leads than the ones already stored in its local DB (`~/.openoutfind`), not N total. Running `find 15` multiple times grows the store.
- We only ever run the **free discovery** command. The command never includes `emails` or `--emails`, so no credits are spent.
- OpenOutreach logs/progress go to **stderr**; results go to **stdout** (shown in Debug Mode).
- The database (`rox.db`) is created automatically on first run. It stores campaigns, leads, enrichment snapshots, and email drafts for testing.