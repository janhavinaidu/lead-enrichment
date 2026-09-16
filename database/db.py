"""SQLite database operations for ROX Test.

Stores campaign, lead, enrichment, and email records for testing
and persistence purposes only.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models.schemas import Campaign, Lead

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rox.db")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    market TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    name TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    linkedin_url TEXT NOT NULL DEFAULT '',
    relevance_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES lead(id),
    profile_json TEXT NOT NULL DEFAULT '{}',
    email TEXT NOT NULL DEFAULT '',
    enriched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES lead(id),
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_cache (
    cache_key TEXT PRIMARY KEY,
    event_name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    num_leads INTEGER NOT NULL DEFAULT 0,
    leads_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create all tables if they do not exist."""
    with get_connection() as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Campaign
# ----------------------------------------------------------------------------

def create_campaign(event_name: str, market: str) -> int:
    """Insert a campaign and return its id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO campaign (event_name, market, created_at) VALUES (?, ?, ?)",
            (event_name, market, utc_now()),
        )
        return int(cursor.lastrowid)


def get_campaign(campaign_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM campaign WHERE id = ?", (campaign_id,)).fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------------
# Lead
# ----------------------------------------------------------------------------

def insert_leads(campaign_id: int, leads: List[Lead]) -> List[int]:
    """Insert leads and return their row ids (in order)."""
    ids: List[int] = []
    with get_connection() as conn:
        for lead in leads:
            cursor = conn.execute(
                """INSERT INTO lead (campaign_id, name, company, title, linkedin_url, relevance_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    campaign_id,
                    lead.name,
                    lead.company,
                    lead.title,
                    lead.linkedin_url,
                    lead.reason,
                ),
            )
            ids.append(int(cursor.lastrowid))
    return ids


def get_leads(campaign_id: int) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM lead WHERE campaign_id = ? ORDER BY id", (campaign_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_lead(lead_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM lead WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def latest_campaign() -> Optional[Dict[str, Any]]:
    """Return the most recent campaign (useful for resuming without re-running discovery)."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM campaign ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------------
# Enrichment
# ----------------------------------------------------------------------------

def insert_enrichment(lead_id: int, profile_json: str, email: str) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO enrichment (lead_id, profile_json, email, enriched_at) VALUES (?, ?, ?, ?)",
            (lead_id, profile_json, email, utc_now()),
        )
        return int(cursor.lastrowid)


def get_enrichment(lead_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM enrichment WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------------

def insert_email(lead_id: int, subject: str, body: str, status: str = "pending") -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO email (lead_id, subject, body, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, subject, body, status, utc_now()),
        )
        return int(cursor.lastrowid)


def get_email(lead_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email WHERE lead_id = ? ORDER BY id DESC LIMIT 1", (lead_id,)
        ).fetchone()
        return dict(row) if row else None


def update_email_status(email_id: int, status: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE email SET status = ? WHERE id = ?", (status, email_id))


# ----------------------------------------------------------------------------
# Discovery cache (persistent Google/LLM lead-search results)
# ----------------------------------------------------------------------------

def save_discovery_cache(
    cache_key: str,
    event_name: str,
    market: str,
    product: str,
    num_leads: int,
    leads_json: str,
) -> None:
    """Persist lead-discovery results keyed by the normalized search inputs."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO discovery_cache
                   (cache_key, event_name, market, product, num_leads, leads_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   event_name = excluded.event_name,
                   market = excluded.market,
                   product = excluded.product,
                   num_leads = excluded.num_leads,
                   leads_json = excluded.leads_json,
                   created_at = excluded.created_at""",
            (cache_key, event_name, market, product, num_leads, leads_json, utc_now()),
        )


def get_discovery_cache(cache_key: str) -> Optional[Dict[str, Any]]:
    """Return a cached discovery result for a cache key, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM discovery_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return dict(row) if row else None


def clear_discovery_cache() -> None:
    """Drop all cached discovery results."""
    with get_connection() as conn:
        conn.execute("DELETE FROM discovery_cache")