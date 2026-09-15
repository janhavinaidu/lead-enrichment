"""Groq API integration service.

Handles two Groq tasks:
1. Generating OpenOutreach targeting instructions from an event + market.
2. Generating personalized outreach emails from a lead profile.
"""

import json
import os
import re
from typing import Any, Dict, Optional

from groq import Groq

from models.schemas import EmailDraft, TargetingInstructions

DEFAULT_MODEL = "openai/gpt-oss-120b"

TARGETING_SYSTEM_PROMPT = """You are a lead-generation targeting specialist.
You convert an event name and a market into precise targeting instructions that a
lead-discovery tool can use to qualify relevant professionals.

Return plain text, no markdown formatting. Be specific about the roles,
industries, and attributes to prioritize."""


def has_api_key() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def get_client() -> Optional[Groq]:
    """Return a Groq client, or None if the API key is missing."""
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    return Groq(api_key=key)


def generate_targeting_instructions(event_name: str, market: str) -> TargetingInstructions:
    """Convert event + market into targeting instructions for OpenOutreach."""
    client = get_client()
    if client is None:
        raise RuntimeError("Groq API key is missing. Add GROQ_API_KEY to your .env file.")

    user_prompt = f"""Event: {event_name}
Market: {market}

Generate targeting instructions for finding relevant leads for this event and market."""

    try:
        completion = client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
            messages=[
                {"role": "system", "content": TARGETING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
        )
        instructions = completion.choices[0].message.content or ""
        return TargetingInstructions(instructions=instructions.strip(), prompt_sent=user_prompt)
    except Exception as exc:
        raise RuntimeError(f"Groq API error:\n{exc}") from exc


def generate_personalized_email(
    event_name: str,
    market: str,
    lead_name: str,
    company: str,
    title: str,
    relevance_reason: str,
    profile_text: str,
    email: str = "",
    to_email: str = "",
) -> EmailDraft:
    """Generate a personalized outreach email for a single lead.

    Returns an EmailDraft with (optionally) parsed subject. If Groq does not
    return a subject line, one is synthesized from the lead name + company.
    """
    client = get_client()
    if client is None:
        raise RuntimeError("Groq API key is missing. Add GROQ_API_KEY to your .env file.")

    system_prompt = """You are a B2B sales development copywriter.
You write a short, professional, personalized outreach email for a lead.
Rules:
- Mention one genuinely relevant detail from the lead profile and connect it to the event/market.
- Do not make up facts.
- Do not mention that LinkedIn or any scraping was used.
- Do not use generic phrases like "I hope this email finds you well."
- Keep it concise (under 150 words).
- Avoid excessive sales language.

Return your response in this exact JSON format:
{"subject": "The subject line", "body": "The email body"}"""

    profile_block = profile_text if profile_text else "(no profile details available)"

    user_prompt = (
        f"Event: {event_name}\n"
        f"Market: {market}\n\n"
        f"Lead:\n"
        f"- Name: {lead_name}\n"
        f"- Company: {company}\n"
        f"- Title: {title}\n"
        f"- Why relevant: {relevance_reason}\n"
        f"- Email: {email or '(not available)'}\n\n"
        f"Profile:\n{profile_block}\n\n"
        f"Write the personalized outreach email as JSON."
    )

    try:
        completion = client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )
        content = completion.choices[0].message.content or ""
    except Exception as exc:
        raise RuntimeError(f"Groq API error:\n{exc}") from exc

    subject, body = _parse_email_json(content, lead_name, company)

    draft = EmailDraft(
        subject=subject,
        body=body,
        to=to_email or email,
        prompt_sent=user_prompt,
    )
    return draft


def _parse_email_json(content: str, lead_name: str, company: str):
    """Best-effort parse of Groq's JSON response into (subject, body)."""
    subject = ""
    body = content
    try:
        data = json.loads(content)
        subject = str(data.get("subject", "")).strip()
        body = str(data.get("body", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                subject = str(data.get("subject", "")).strip()
                body = str(data.get("body", "")).strip()
            except (json.JSONDecodeError, AttributeError):
                pass

    if not subject:
        # Simple fallback subject
        subject = f"Regarding {company} and the upcoming event" if company else "Quick note"
    return subject, body