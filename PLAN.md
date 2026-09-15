# ROX Test - Implementation Plan

## Overview
A Streamlit-based prototype to test the complete lead-generation pipeline: Groq → OpenOutreach → Apify → Groq email generation.

## Project Structure
```
testing_mvp/
├── app.py                    # Main Streamlit application
├── requirements.txt          # Python dependencies
├── .env.example             # Environment variable template
├── .gitignore               # Git ignore rules
├── README.md                # Setup and usage documentation
├── services/
│   ├── __init__.py
│   ├── groq_service.py      # Groq API integration
│   ├── openoutreach_service.py  # OpenOutreach CLI wrapper
│   ├── apify_service.py     # Apify Actor runners
│   └── email_service.py     # Email placeholder
├── database/
│   ├── __init__.py
│   └── db.py                # SQLite database operations
└── models/
    ├── __init__.py
    └── schemas.py           # Data models/schemas
```

## Implementation Steps

### Phase 1: Foundation
1. Create directory structure
2. Create `.env.example` with required variables
3. Create `requirements.txt`
4. Create `.gitignore`
5. Create `models/schemas.py` with data classes
6. Create `database/db.py` with SQLite operations

### Phase 2: Services
7. Create `services/groq_service.py` - Groq API wrapper
8. Create `services/openoutreach_service.py` - OpenOutreach CLI subprocess wrapper
9. Create `services/apify_service.py` - Apify Actor runners
10. Create `services/email_service.py` - Placeholder

### Phase 3: Main Application
11. Create `app.py` with full Streamlit UI and pipeline logic

### Phase 4: Documentation
12. Create comprehensive `README.md`

## Key Technical Decisions

### OpenOutreach Integration
- Use subprocess wrapper to call `openoutreach find N --json`
- Parse JSON Lines output from stdout
- Capture stderr for debugging
- Environment variables: `OPENOUTFIND_PRODUCT_DOCS`, `OPENOUTFIND_CAMPAIGN_TARGET`, `OPENOUTFIND_AI_MODEL`, `OPENOUTFIND_LLM_API_KEY`, `OPENOUTFIND_BETTERCONTACT_API_KEY`, `OPENOUTFIND_OPERATOR_EMAIL`, `OPENOUTFIND_OPERATOR_COUNTRY`, `OPENOUTFIND_ACCEPT_LEGAL_NOTICE`

### Apify Integration
- Use `apify-client` Python package
- Two separate Actor functions: `run_apify_search_actor()` and `run_apify_profile_actor()`
- Actor IDs configurable via `.env`

### Groq Integration
- Use `groq` Python package
- Model: `llama-3.3-70b-versatile` (fast, capable)
- Two use cases: targeting generation and email personalization

### Database
- SQLite with 4 tables: Campaign, Lead, Enrichment, Email
- Simple CRUD operations
- Auto-create tables on first run

## Success Criteria
- All API integrations work with real credentials
- Complete pipeline: event+market → targeting → leads → selection → enrichment → email
- Clear error handling for all failure cases
- Debug mode shows all intermediate data
