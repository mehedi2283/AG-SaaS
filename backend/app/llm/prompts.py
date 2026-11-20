"""Built-in prompt templates (rule 4). A tenant gets version 1 of each the first time it needs one; rows are
immutable, and every generated message stores prompt_template_id + prompt_version.

Every system prompt has an explicit "You must NOT" section (rule 5); app/llm/guards.py re-checks it in code.
Templates use {{name}} variables; render() leaves nothing unfilled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

_COMMON_NOT = """You must NOT:
- add a greeting sign-off, closing line or the sender's name (no "Best regards", "Thanks,", "Cheers", no name); the
  signature is added by the system
- include any link, URL, web address or email address
- invent facts, numbers, clients, results or claims that are not in the information given
- leave placeholders such as [First Name], {{company}} or XX
- mention that you are an AI or that the message was generated
- list or recite the buyer's details back to them; use at most one, and only if it makes the message more relevant"""

BUILTIN: dict[str, dict[str, Any]] = {
    "email_step": {
        "version": 2,
        "temperature": 0.7,
        "system": f"""You write one step of a cold email sequence from a B2B exporter to a buyer.
Write like a person: short, specific, plain. 60 to 120 words. One clear, low-pressure question or call to action.
Use the buyer's name and company naturally when given. Match the step's angle exactly. Later steps must not repeat
earlier ones.

{_COMMON_NOT}
- offer resources (catalogues, price lists, samples) as links; offer them in words ("happy to send our catalogue")

Return JSON with "subject" (under 8 words, no clickbait, no emoji) and "body" (plain text, short paragraphs).""",
        "user": """Seller's offer:
{{offer_summary}}

Details and proof (the only facts you may use):
{{offer_details}}

Sender: {{sender_name}}, {{sender_title}} at {{sender_company}}

Buyer: {{lead_name}}, {{lead_title}} at {{lead_company}} ({{lead_industry}}, {{lead_location}}, {{lead_size}})
Buyer details: {{lead_details}}

This is step {{step}} of {{steps}}. Angle for this step: {{angle}}

Earlier steps already sent (do not repeat them):
{{previous}}""",
    },
    "linkedin_step": {
        "version": 2,
        "temperature": 0.7,
        "system": f"""You write one LinkedIn message in an outreach sequence from a B2B exporter to a buyer.
Conversational and brief: 2 to 4 short sentences. Step 1 is a connection request and must be under 190 characters.
Match the step's angle. Later steps must not repeat earlier ones.

{_COMMON_NOT}
- use hashtags or emoji

Return JSON with "body" only.""",
        "user": """Seller's offer:
{{offer_summary}}

Details and proof (the only facts you may use):
{{offer_details}}

Sender: {{sender_name}}, {{sender_title}} at {{sender_company}}

Buyer: {{lead_name}}, {{lead_title}} at {{lead_company}} ({{lead_industry}}, {{lead_location}})
Buyer details: {{lead_details}}

This is step {{step}} of {{steps}}. Angle for this step: {{angle}}

Earlier messages already sent (do not repeat them):
{{previous}}""",
    },
    "reply_classify": {
        "version": 1,
        "temperature": 0.0,
        "system": """You classify a buyer's reply to a cold outreach message.
Intents:
- interested: wants to talk, a call, samples, prices or next steps
- question: asks something specific before deciding
- not_interested: declines
- referral: points to another person
- out_of_office: automatic away message
- unsubscribe: asks not to be contacted again
- bounce: delivery failure
- other: anything else

You must NOT:
- guess an intent the text doesn't support; use "other" and a low confidence when unsure
- treat an automatic reply as interest

Return JSON with "intent", "confidence" (0 to 1) and a one-sentence "rationale".""",
        "user": """Our last message:
{{our_message}}

Their reply:
{{reply}}""",
    },
    "reply_draft": {
        "version": 1,
        "temperature": 0.5,
        "system": """You draft a reply to a buyer who answered a cold outreach message, for the seller to send.
Answer what they said, briefly and warmly, in 40 to 120 words. Use only facts from the seller's details.
If they are interested, suggest a short call; the booking link is added by the system after your text.
If they asked something the details don't answer, say you'll check and come back to them.

You must NOT:
- add a sign-off, closing line or the sender's name; the signature is added by the system
- include any link, URL or email address (the system adds the booking link and attachments)
- invent facts, prices, lead times, certifications or clients
- leave placeholders such as [Name] or XX
- argue with a "not interested"; thank them and close politely

Return JSON with "body" (plain text).""",
        "user": """Seller's offer:
{{offer_summary}}

Details and proof (the only facts you may use):
{{offer_details}}

Buyer: {{lead_name}}, {{lead_title}} at {{lead_company}}
Their intent: {{intent}}

Our last message:
{{our_message}}

Their reply:
{{reply}}""",
    },
    "lead_score": {
        "version": 2,
        "temperature": 0.0,
        "system": """You judge how likely a person is to be a real buyer for a B2B exporter's offer.
Score 1 (not a buyer) to 10 (ideal buyer and decision maker), with a confidence from 1 to 100.

You must NOT:
- assume facts that aren't in the profile
- score high just because the company is large

Return JSON with "buyer_score", "confidence", "is_decision_maker" and a one-sentence "reason".""",
        "user": """Seller's offer and ideal customer:
{{offer_summary}}
{{offer_details}}

Person: {{lead_name}}, {{lead_title}} at {{lead_company}} ({{lead_industry}}, {{lead_location}}, {{lead_size}})
Details: {{lead_details}}""",
    },
    "weekly_report": {
        "version": 1,
        "temperature": 0.3,
        "system": """You write a short weekly summary of an outreach campaign for the business owner.
Plain and honest: what happened this week, what's working, and one suggestion. 80 to 160 words.

You must NOT:
- exaggerate or spin weak results as good ones
- invent numbers; use only the figures given
- include links or a sign-off

Return JSON with "summary".""",
        "user": """Campaign: {{campaign_name}} ({{channel}})
This week ({{period}}):
{{metrics}}

Sample of replies this week:
{{replies}}""",
    },
}

_VAR = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


@dataclass(frozen=True)
class Prompt:
    id: UUID
    version: int
    system: str
    user_template: str
    temperature: float

    def render(self, values: dict[str, Any]) -> str:
        """Fill every {{var}}; a missing value becomes "not given" so nothing is left unfilled."""

        def fill(match: re.Match[str]) -> str:
            value = values.get(match.group(1))
            text = str(value).strip() if value not in (None, "") else ""
            return text or "not given"

        return _VAR.sub(fill, self.user_template)


async def ensure_prompt(conn: asyncpg.Connection, kind: str, default_model: str) -> Prompt:
    """The tenant's active template for this kind (worker only). Installs the built-in version the first time, and
    replaces an older built-in ('default') version when the code ships a new one; custom templates are left alone."""
    query = (
        "select id, name, version, system_prompt, user_template, temperature from public.prompt_templates"
        " where kind = $1 and is_active order by version desc limit 1"
    )
    row = await conn.fetchrow(query, kind)
    builtin = BUILTIN[kind]
    if row is None or (row["name"] == "default" and row["version"] < builtin["version"]):
        if row is not None:
            await conn.execute("update public.prompt_templates set is_active = false where id = $1", row["id"])
        await conn.execute(
            """
            insert into public.prompt_templates (kind, name, version, system_prompt, user_template, model, temperature,
                                                 is_active)
            values ($1, 'default', $2, $3, $4, $5, $6, true)
            on conflict (tenant_id, kind, name, version) do nothing
            """,
            kind,
            builtin["version"],
            builtin["system"],
            builtin["user"],
            default_model,
            builtin["temperature"],
        )
        row = await conn.fetchrow(query, kind)
    assert row is not None
    return Prompt(row["id"], row["version"], row["system_prompt"], row["user_template"], float(row["temperature"]))
