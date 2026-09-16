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
import zlib
from typing import List, Optional
import requests
from groq import Groq

from models.schemas import Lead

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_WORKERS = 8  # Parallel worker threads for fast search

# Geography rotation: Google Custom Search ranks results by a geolocation.
# Without an explicit `gl`, Google infers location from the requester IP, which
# clusters every search into one country (e.g. India). Assigning each target a
# rotating, deterministic geolocation spreads coverage across global markets.
GEO_CYCLE = ["us", "gb", "de", "ae", "sg", "au", "ca", "nl", "fr"]

_TARGETS_SYSTEM_PROMPT = """\
You are an expert global B2B lead generation specialist.
Given an event name, market/industry, and optionally a product/solution being sold,
return a JSON array of the most relevant target profiles to reach out to.

Each item in the array must have exactly two string keys:
  "company"  — the target company name (real, well-known relevant companies globally)
  "role"     — the exact job title to target (e.g. "Chief Procurement Officer", "VP of Engineering", "Head of Operations")

Rules:
- Return ONLY valid JSON array. No markdown, no explanation.
- Return exactly the number of items requested (default: 20 unique pairs).
- Focus strictly on RELEVANCE to the event, market, or product being sold. Select the top relevant companies and decision-makers GLOBALLY.
- Never default to, or cluster picks in, any single country (especially the user's own local market). Spread selections across multiple regions and markets — e.g. North America, Europe, Middle East, Asia-Pacific, Latin America, Africa — in rough proportion to genuine buying relevance.
- Do NOT bias toward any specific country such as India, just because the user is based there. Only include a country's companies if they are objectively the most relevant.
- Each (company, role) pair must be UNIQUE — do not repeat the same company+role.
- Do not repeat the same company more than 2 times total.
- Prefer senior decision-makers: C-suite, VP, Head of, Director, GM.
- If a product/solution is provided, target key decision-makers who buy or use that product at target companies worldwide.
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

            gl = _geolocation_for(company, role)

            if use_google:
                future = executor.submit(_search_target, company, role, g_key, g_cx, gl)
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

            # Prefer the candidate that most strongly matches the target
            # company + role RIGHT NOW. Drop results that look like ex-/former
            # employees of the company (negative score).
            ranked = sorted(
                (r for r in results if r.get("linkedin_url") and r.get("score", 0.0) >= 0.0),
                key=lambda r: r.get("score", 0.0),
                reverse=True,
            )
            # Pick the first unique LinkedIn profile URL per target
            for result in ranked:
                url = result["linkedin_url"]
                if url in seen_urls:
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


def _geolocation_for(company: str, role: str) -> str:
    """Pick a deterministic geolocation per target so parallel searches spread
    across global markets instead of all clustering in one country."""
    key = f"{company.lower()} | {role.lower()}"
    return GEO_CYCLE[zlib.crc32(key.encode("utf-8")) % len(GEO_CYCLE)]


def _search_target(company: str, role: str, g_key: str, g_cx: str, gl: str = "") -> List[dict]:
    """Try Google Custom Search first. If blocked/failed/empty, fall back to DDG."""
    results = _search_linkedin_google(company, role, g_key, g_cx, gl=gl)
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

def _search_linkedin_google(company: str, role: str, api_key: str, cse_id: str, gl: str = "") -> List[dict]:
    """Search Google Custom Search JSON API for LinkedIn profiles matching company + role."""
    queries = [
        f'site:linkedin.com/in "{company}" "{role}"',
        f'site:linkedin.com/in "{company}" {role}',
        f'site:linkedin.com/in {company} {role}',
    ]

    for query in queries:
        try:
            params = {
                "key": api_key,
                "cx": cse_id,
                "q": query,
                "num": 5,
            }
            if gl:
                # Explicit geolocation counteracts Google's IP-inferred
                # geolocation bias (which otherwise clusters results in the
                # requester's local country).
                params["gl"] = gl

            resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params=params,
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
                snippet_text = item.get("snippet", "")
                first_name, last_name = _extract_name(url, title)
                results.append({
                    "linkedin_url": url,
                    "first_name": first_name,
                    "last_name": last_name,
                    "title": title,
                    "snippet": snippet_text,
                    "score": _match_score(company, role, title, snippet_text),
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
        f'site:linkedin.com/in "{company}" "{role}"',
        f'site:linkedin.com/in "{company}" {role}',
        f'site:linkedin.com/in {company} {role}',
    ]

    for query in queries:
        raw = []
        try:
            with DDGS() as ddgs:
                # region="wt-wt" = worldwide; avoids DDG region-localized results.
                raw = list(ddgs.text(query, region="wt-wt", max_results=5))
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
            snippet_text = item.get("body", "")
            first_name, last_name = _extract_name(url, title)
            results.append({
                "linkedin_url": url,
                "first_name": first_name,
                "last_name": last_name,
                "title": title,
                "snippet": snippet_text,
                "score": _match_score(company, role, title, snippet_text),
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


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "at", "in", "is", "to", "by",
    "inc", "co", "ltd", "llc", "gmbh", "corp", "plc", "limited", "ag", "sa",
    "pvt", "private", "solutions", "company", "group", "holdings", "holding",
    "llp", "corporation", "international", "global", "worldwide",
}


def _significant_tokens(phrase: str) -> set:
    """Lowercased, meaningful word tokens of a company/role phrase."""
    words = re.split(r"[^a-z0-9]+", phrase.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _match_score(company: str, role: str, title: str, snippet: str) -> float:
    """Score how well a search result matches a CURRENT (company, role) target.

    A LinkedIn search result title carries the headline, e.g.
    "Ravi Kumar - Head of Procurement - Tata Motors | LinkedIn". Scoring the
    title + snippet boosts candidates who are at the company right now and
    penalizes ex-/former- markers attached to the target role or company —
    the people who "are Head/Chief of X at Company" but no longer are.
    """
    text = (f"{title or ''} {snippet or ''}").lower()
    score = 0.0

    # Past marker ("ex", "former", "previous", "past") directly attached to a
    # target role token -> e.g. "Ex-Head of Procurement", "Former Head of Sales".
    role_tokens = _significant_tokens(role)
    past_marker = r"\b(?:ex[\s-]*|former[\s-]*|previous[\s-]*|past[\s-]*)"
    if any(re.search(past_marker + re.escape(t), text) for t in role_tokens):
        score -= 8.0

    # Past marker directly attached to the target company
    # -> e.g. "Former Tata Motors employee".
    comp = (company or "").strip()
    if comp:
        comp_pat = re.escape(comp.lower())
        if re.search(past_marker + comp_pat, text):
            score -= 8.0

    for phrase in (company, role):
        tokens = _significant_tokens(phrase)
        if not tokens:
            continue
        hits = sum(1 for t in tokens if t in text)
        if hits:
            score += 2.0 * hits / len(tokens)
            if hits == len(tokens):
                score += 1.5  # full phrase matched bonus

    return score


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
