"""Turning written messages into what the sending layer gets: HTML paragraphs plus the signature (added by code;
the model never writes a sign-off). The signature <img> is the only URL allowed in follow-ups (rule 6)."""

from __future__ import annotations

import html
import re

_HAS_TAGS = re.compile(r"<(p|br|div|ul|ol|li|strong|em|b|i)\b", re.I)


def to_html(text: str) -> str:
    """Plain text -> <p> paragraphs with <br> for single line breaks. Text that is already HTML is kept."""
    if _HAS_TAGS.search(text):
        return text
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def signature_block(signature: str | None, image_url: str | None, *, html_mode: bool = True) -> str:
    if not html_mode:
        return f"\n\n{signature.strip()}" if signature else ""
    parts = []
    if signature:
        parts.append(to_html(signature) if not _HAS_TAGS.search(signature) else signature)
    if image_url and image_url.startswith("https://"):
        parts.append(f'<img src="{html.escape(image_url, quote=True)}" alt="Signature" width="600" />')
    return "".join(parts)


def email_body(text: str, signature: str | None, image_url: str | None) -> str:
    return to_html(text) + signature_block(signature, image_url)


def linkedin_body(text: str) -> str:
    """LinkedIn messages are plain text; no signature (the profile is the signature)."""
    return text.strip()
