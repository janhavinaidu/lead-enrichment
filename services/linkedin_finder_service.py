"""Fast & Accurate LinkedIn lead discovery service.

2-step pipeline:

  Step 1 — Groq LLM generates a list of (company, role) pairs relevant to the
            given event / market / product.

  Step 2 — Search for each target (company, role) pair concurrently using
            Google Custom Search API (with automatic DuckDuckGo fallback).
            Extracts exact linkedin.com/in/... profile URLs.

Uses concurrent.futures.ThreadPoolExecutor for ultra-fast parallel discovery.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
from typing import List, Optional
import requests
from groq import Groq

from models.schemas import Lead

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_WORKERS = 8  # Parallel worker threads for fast search

_TARGETS_SYSTEM_PROMPT = """\
You are a B2B lead generation specialist based in India.
Given an event name, market/industry, and optionally a product/solution being sold,
return a JSON array of the most relevant target profiles to reach out to.

Each item in the array must have exactly two string keys:
  "company"  — the target company name (real, well-known companies)
  "role"     — the exact job title to target (e.g. "Chief Procurement Officer")

Rules:
- Return ONLY valid JSON array. No markdown, no explanation.
- Return exactly the number of items requested (default: 20 unique pairs).
- STRONGLY prefer Indian companies and Indian subsidiaries of global companies.
  At least 70% of companies must be Indian (e.g. Tata Motors, Maruti Suzuki,
  Mahindra, Bajaj Auto, Hero MotoCorp, Ola, Hyundai India, MG Motor India, etc.)
- Each (company, role) pair must be UNIQUE — do not repeat the same company+role.
- Do not repeat the same company more than 2 times total.
- Prefer senior decision-makers: C-suite, VP, Head of, Director, GM.
- If a product/solution is provided, target the people who make buying decisions
  for that product at companies that realistically purchase it.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_leads(
    event_name: str,
    market: str,
    num_leads: int = 15,
    product: str = "",
    groq_api_key: Optional[str] = None,
    google_api_key: Optional[str] = None,
    google_cse_id: Optional[str] = None,
) -> List[Lead]:
    """Discover leads using LLM targeting + Parallel Google / DDG LinkedIn search.

    Args:
        event_name: Name of the event (e.g. "IDA 2026").
        market: Market / industry (e.g. "Automobile").
        num_leads: Number of leads to return.
        product: Optional product/solution being sold (e.g. "electric car").
        groq_api_key: Override for GROQ_API_KEY env var.
        google_api_key: Override for GOOGLE_API_KEY env var.
        google_cse_id: Override for GOOGLE_CSE_ID env var.

    Returns:
        List of Lead objects with linkedin_url, name, company, title populated.
    """
    api_key = groq_api_key or os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to your .env file.")

    g_key = google_api_key or os.getenv("GOOGLE_API_KEY", "").strip()
    g_cx = google_cse_id or os.getenv("GOOGLE_CSE_ID", "").strip()
    use_google = bool(g_key and g_cx)

    target_count = max(num_leads * 2, 20)
    targets = _generate_targets(event_name, market, api_key, product=product, count=target_count)

    leads: List[Lead] = []
    seen_urls: set = set()

    # Parallelize target search execution using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_target = {}
        for target in targets:
            company = target.get("company", "").strip()
            role = target.get("role", "").strip()
            if not company or not role:
                continue

            if use_google:
                future = executor.submit(_search_target, company, role, g_key, g_cx)
            else:
                future = executor.submit(_search_linkedin_ddg, company, role)

            future_to_target[future] = (company, role)

        for future in as_completed(future_to_target):
            if len(leads) >= num_leads:
                # Early stop once requested lead threshold is met
                break

            company, role = future_to_target[future]
            try:
                results = future.result()
            except Exception:
                results = _search_linkedin_ddg(company, role)

            # Extract the FIRST valid unique LinkedIn profile URL per target
            for result in results:
                url = result.get("linkedin_url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                leads.append(Lead(
                    first_name=result.get("first_name", ""),
                    last_name=result.get("last_name", ""),
                    company=company,
                    title=role,
                    linkedin_url=url,
                    reason=_build_reason(role, company, event_name, market, product),
                ))
                break  # 1 lead per target

    return leads[:num_leads]


def _search_target(company: str, role: str, g_key: str, g_cx: str) -> List[dict]:
    """Try Google Custom Search first. If blocked/failed/empty, fall back to DDG."""
    results = _search_linkedin_google(company, role, g_key, g_cx)
    if not results:
        # Automatic fallback to DDG if Google returned 0 results or permission error
        results = _search_linkedin_ddg(company, role)
    return results


# ---------------------------------------------------------------------------
# Step 1 — LLM target generation
# ---------------------------------------------------------------------------

def _generate_targets(
    event_name: str,
    market: str,
    api_key: str,
    product: str = "",
    count: int = 20,
) -> List[dict]:
    """Ask Groq to return a list of (company, role) dicts."""
    client = Groq(api_key=api_key)

    user_prompt = f"Generate {count} unique (company, role) pairs.\n"
    if event_name:
        user_prompt += f"Event: {event_name}\n"
    if market:
        user_prompt += f"Market / Industry: {market}\n"
    if product:
        user_prompt += f"Product / Solution being sold: {product}\n"
    user_prompt += "\nReturn ONLY the JSON array."

    try:
        completion = client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
            messages=[
                {"role": "system", "content": _TARGETS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )
        content = completion.choices[0].message.content or "[]"
        return _parse_targets_json(content)
    except Exception as exc:
        raise RuntimeError(f"Groq target generation failed: {exc}") from exc


def _parse_targets_json(content: str) -> List[dict]:
    """Best-effort parse of LLM JSON output."""
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
    return []


# ---------------------------------------------------------------------------
# Step 2 — Search Engine Integrations (Google Custom Search API & DDG Fallback)
# ---------------------------------------------------------------------------

def _search_linkedin_google(company: str, role: str, api_key: str, cse_id: str) -> List[dict]:
    """Search Google Custom Search JSON API for LinkedIn profiles matching company + role."""
    queries = [
        f'site:linkedin.com/in "{company}" {role}',
        f'site:linkedin.com/in {company} {role}',
    ]

    for query in queries:
        try:
            resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": api_key,
                    "cx": cse_id,
                    "q": query,
                    "num": 3,
                },
                timeout=5,
            )
            if resp.status_code != 200:
                # Log error status code for diagnosis
                print(f"[Google CSE Warning] HTTP {resp.status_code}: {resp.text[:200]}")
                continue

            data = resp.json()
            items = data.get("items", [])
            results = []

            for item in items:
                link = item.get("link", "")
                url = _extract_linkedin_url(link)
                if not url:
                    snippet = item.get("snippet", "") + " " + link
                    url = _extract_linkedin_url(snippet)
                if not url:
                    continue

                title = item.get("title", "")
                first_name, last_name = _extract_name(url, title)
                results.append({
                    "linkedin_url": url,
                    "first_name": first_name,
                    "last_name": last_name,
                })

            if results:
                return results
        except Exception as exc:
            print(f"[Google CSE Error] {exc}")
            continue

    return []


def _search_linkedin_ddg(company: str, role: str) -> List[dict]:
    """Fallback: Search DuckDuckGo for LinkedIn profiles using consolidated query templates."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        return []

    queries = [
        f'site:linkedin.com/in "{company}" {role}',
        f'site:linkedin.com/in {company} {role}',
    ]

    for query in queries:
        raw = []
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=3))
        except Exception:
            continue

        results = []
        for item in raw:
            href = item.get("href", "")
            url = _extract_linkedin_url(href)
            if not url:
                body = item.get("body", "") + " " + item.get("href", "")
                url = _extract_linkedin_url(body)
            if not url:
                continue
            title = item.get("title", "")
            first_name, last_name = _extract_name(url, title)
            results.append({
                "linkedin_url": url,
                "first_name": first_name,
                "last_name": last_name,
            })

        if results:
            return results

    return []


def _extract_linkedin_url(href: str) -> str:
    """Return a clean linkedin.com/in/... URL or empty string."""
    if not href:
        return ""
    match = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?&#\s]+)", href)
    if match:
        return f"https://www.linkedin.com/in/{match.group(1)}"
    return ""


def _extract_name(linkedin_url: str, title: str) -> tuple:
    """Best-effort extract (first_name, last_name) from search title or URL slug."""
    if title:
        clean = re.split(r"\s*[\-–|]\s*", title)[0].strip()
        if clean and len(clean.split()) >= 2 and not any(
            kw in clean.lower()
            for kw in ("linkedin", "profile", "view", "connect", "jobs")
        ):
            parts = clean.split()
            return parts[0], " ".join(parts[1:])

    match = re.search(r"linkedin\.com/in/([^/?&#]+)", linkedin_url)
    if match:
        slug = match.group(1)
        slug = re.sub(r"-[a-z0-9]{6,}$", "", slug)
        parts = [p.capitalize() for p in slug.split("-") if p]
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        elif len(parts) == 1:
            return parts[0], ""

    return "", ""


def _build_reason(role: str, company: str, event_name: str, market: str, product: str) -> str:
    """Build a clean 'Why Relevant' string, handling empty event/market."""
    parts = [f"{role} at {company}"]
    context = []
    if event_name:
        context.append(event_name)
    if market:
        context.append(market)
    if context:
        parts.append(f"| {' / '.join(context)}")
    if product:
        parts.append(f"| buyer of {product}")
    return " ".join(parts)
