"""Reading Instantly / HeyReach webhook payloads into one shape. Pure functions, unit-tested.

Field names come from the n8n workflows and Instantly's/HeyReach's docs (docs/status.md). Both spellings Instantly
has used are accepted; verify against a real event before going live.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<\s*(br|/p|/div)\s*/?>", re.I)
# Quoted history below the reply ("On Tue, ... wrote:", "-----Original Message-----", "> ...").
_QUOTE = re.compile(r"(\n\s*On .{5,120}wrote:\s*\n|\n-{2,}\s*Original Message\s*-{2,}|\n\s*From: .+\n\s*Sent: )", re.I)


@dataclass
class Inbound:
    kind: str  # "reply" | "bounce" | "unsubscribe" | "ignore"
    channel: str
    provider_message_id: str
    campaign_ref: str | None
    lead_email: str | None = None
    lead_profile_url: str | None = None
    lead_name: str | None = None
    subject: str | None = None
    body: str = ""
    auto_reply: bool = False
    thread_ref: dict[str, Any] = field(default_factory=dict)


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def html_to_text(value: str) -> str:
    text = _TAGS.sub("", _BREAKS.sub("\n", value))
    return html.unescape(text)


def strip_quoted(text: str) -> str:
    """The new part of an email reply, without the quoted thread underneath."""
    match = _QUOTE.search("\n" + text)
    body = text[: max(match.start() - 1, 0)] if match else text
    lines = [line for line in body.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def parse_instantly(payload: dict[str, Any]) -> Inbound:
    event = str(payload.get("event_type") or payload.get("event") or "reply_received").lower()
    raw_body = payload.get("reply_text") or payload.get("body")
    if isinstance(raw_body, dict):
        raw_body = raw_body.get("text") or html_to_text(str(raw_body.get("html") or ""))
    if not raw_body and payload.get("reply_html"):
        raw_body = html_to_text(str(payload["reply_html"]))
    message_id = payload.get("email_id") or payload.get("id") or payload.get("message_id") or ""
    kind = "reply"
    if "bounce" in event:
        kind = "bounce"
    elif "unsubscribe" in event:
        kind = "unsubscribe"
    elif "reply" not in event:
        kind = "ignore"
    return Inbound(
        kind=kind,
        channel="email",
        provider_message_id=str(message_id),
        campaign_ref=str(payload["campaign_id"]) if payload.get("campaign_id") else None,
        lead_email=(str(payload.get("lead_email") or payload.get("from_email") or "").strip().lower() or None),
        subject=payload.get("reply_subject") or payload.get("subject"),
        body=strip_quoted(str(raw_body or "")),
        auto_reply=bool(payload.get("is_auto_reply")),
        thread_ref={
            "reply_to_uuid": str(message_id),
            "eaccount": payload.get("eaccount") or payload.get("email_account"),
            "thread_id": payload.get("thread_id"),
            "subject": payload.get("reply_subject") or payload.get("subject"),
        },
    )


def parse_heyreach(payload: dict[str, Any]) -> Inbound:
    messages = [m for m in payload.get("recent_messages") or [] if isinstance(m, dict)]
    inbound = [m for m in messages if str(m.get("message_type") or "").upper() not in ("OUTBOUND", "SENT", "ME")]
    latest = max(inbound or messages, key=lambda m: str(m.get("creation_time") or ""), default={})
    lead = payload.get("lead") or {}
    conversation = payload.get("conversation_id")
    return Inbound(
        kind="reply" if latest else "ignore",
        channel="linkedin",
        provider_message_id=f"{conversation}:{latest.get('creation_time', '')}",
        campaign_ref=str(_get(payload, "campaign", "id")) if _get(payload, "campaign", "id") else None,
        lead_profile_url=(lead.get("profile_url") or None),
        lead_name=lead.get("full_name"),
        body=str(latest.get("message") or "").strip(),
        # HeyReach conversations are scoped by the SENDING account (CLAUDE.md gotchas).
        thread_ref={"account_id": str(_get(payload, "sender", "id") or ""), "conversation_id": conversation},
    )


def normalize_profile(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip().lower().split("?")[0].rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", url)
