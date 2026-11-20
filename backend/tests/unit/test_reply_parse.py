from __future__ import annotations

from app.domain.replies.parse import normalize_profile, parse_heyreach, parse_instantly, strip_quoted


def test_instantly_reply_with_quoted_history() -> None:
    inbound = parse_instantly(
        {
            "event_type": "reply_received",
            "campaign_id": "inst-1",
            "email_id": "em-9",
            "lead_email": "Ana@ModaViva.example",
            "email_account": "rahim@gazi.example",
            "reply_subject": "Re: Knitwear",
            "reply_text": "Yes, send samples please.\n\nOn Tue, 3 Mar 2026 at 10:00, Rahim wrote:\n> Hi Ana",
        }
    )
    assert inbound.kind == "reply" and inbound.lead_email == "ana@modaviva.example"
    assert inbound.body == "Yes, send samples please."
    assert inbound.thread_ref == {
        "reply_to_uuid": "em-9",
        "eaccount": "rahim@gazi.example",
        "thread_id": None,
        "subject": "Re: Knitwear",
    }


def test_instantly_n8n_field_names_and_html_body() -> None:
    inbound = parse_instantly(
        {
            "id": "x1",
            "campaign_id": "c",
            "from_email": "b@c.co",
            "eaccount": "me@x.co",
            "body": {"html": "<p>Interested.</p><p>Call me</p>"},
            "is_auto_reply": True,
        }
    )
    assert inbound.body == "Interested.\nCall me" and inbound.auto_reply is True


def test_instantly_bounce_and_other_events() -> None:
    assert parse_instantly({"event_type": "email_bounced", "campaign_id": "c", "lead_email": "a@b.co"}).kind == "bounce"
    assert parse_instantly({"event_type": "email_opened", "campaign_id": "c"}).kind == "ignore"


def test_heyreach_takes_the_latest_inbound_message() -> None:
    inbound = parse_heyreach(
        {
            "campaign": {"id": 77},
            "conversation_id": "conv-1",
            "sender": {"id": 501},
            "lead": {"profile_url": "https://www.linkedin.com/in/ana/", "full_name": "Ana Silva"},
            "recent_messages": [
                {
                    "message": "Hi Ana, thanks for connecting",
                    "creation_time": "2026-03-01T10:00:00Z",
                    "message_type": "OUTBOUND",
                },
                {
                    "message": "Happy to talk next week",
                    "creation_time": "2026-03-02T09:00:00Z",
                    "message_type": "INBOUND",
                },
            ],
        }
    )
    assert inbound.body == "Happy to talk next week" and inbound.campaign_ref == "77"
    assert inbound.thread_ref == {"account_id": "501", "conversation_id": "conv-1"}
    assert inbound.provider_message_id == "conv-1:2026-03-02T09:00:00Z"


def test_profile_urls_compare_loosely() -> None:
    assert normalize_profile("https://www.LinkedIn.com/in/ana/?trk=x") == normalize_profile(
        "http://linkedin.com/in/ana"
    )


def test_strip_quoted_drops_gt_lines() -> None:
    assert strip_quoted("Sounds good\n> earlier\n> more") == "Sounds good"
