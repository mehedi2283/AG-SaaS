from __future__ import annotations

import pytest

from app.llm.guards import check_reply, check_step, has_link
from app.llm.outputs import EmailStep, ReplyClassification, strict_schema
from app.llm.prompts import BUILTIN, Prompt

GOOD = "Hi Ana, I noticed Moda SA is expanding its knitwear range. We make organic cotton jumpers for EU retailers.\n\nWould a short call next week be useful?"


def test_a_clean_email_step_passes() -> None:
    assert check_step("email", 1, "Knitwear for Moda", GOOD, "Rahim Uddin") == []


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (GOOD + "\n\nBest regards,", "sign-off (the signature is added by Gazi)"),
        (GOOD + "\n\nThanks,\nRahim", "sign-off (the signature is added by Gazi)"),
        (GOOD + "\n\nRahim", "sender name at the end"),
        ("Hi [First Name], " + GOOD, "unfilled placeholder"),
        ("Hi {{first_name}}, " + GOOD, "unfilled placeholder"),
        (GOOD + " See https://gazi.example/catalogue", "link in a follow-up"),
        (GOOD + " www.gazi.example", "link in a follow-up"),
        (GOOD + ' <a href="x">catalogue</a>', "link in a follow-up"),
    ],
)
def test_follow_up_violations(body: str, problem: str) -> None:
    assert problem in check_step("email", 2, "Following up", body, "Rahim Uddin")


def test_thanks_mid_sentence_is_not_a_sign_off() -> None:
    body = "Thanks for reading this far, Ana. " + GOOD
    assert check_step("email", 2, "Quick idea", body) == []


def test_signature_image_is_not_a_link() -> None:
    assert not has_link(GOOD + '<img src="https://cdn.example.com/sig.png" alt="Signature">')


def test_linkedin_rules() -> None:
    assert "connection request over 200 characters" in check_step("linkedin", 1, None, "x" * 201)
    assert "LinkedIn messages have no subject" in check_step("linkedin", 2, "Hello", GOOD)
    assert (
        check_step(
            "linkedin", 1, None, "Hi Ana, I work with EU knitwear buyers and saw Moda's new range. Happy to connect?"
        )
        == []
    )


def test_email_needs_a_subject() -> None:
    assert "missing subject" in check_step("email", 1, "", GOOD)


def test_reply_guard_rejects_any_link() -> None:
    assert "link (Gazi adds the calendar link itself)" in check_reply("Great, book here: https://cal.com/x")
    assert check_reply("Great to hear, Ana. Would Tuesday or Wednesday suit you for a 20-minute call?") == []


def test_strict_schema_requires_everything_and_forbids_extras() -> None:
    schema = strict_schema(EmailStep)
    assert schema["required"] == ["subject", "body"] and schema["additionalProperties"] is False
    assert "maxLength" not in str(schema)
    intent = strict_schema(ReplyClassification)["properties"]["intent"]
    assert "interested" in intent["enum"]


def test_every_builtin_prompt_states_its_prohibitions() -> None:
    for kind, prompt in BUILTIN.items():
        assert "must not" in prompt["system"].lower(), kind


def test_render_fills_every_variable() -> None:
    prompt = Prompt(
        id=None, version=1, system="", user_template="Hi {{lead_name}} at {{lead_company}}", temperature=0.5
    )  # type: ignore[arg-type]
    assert prompt.render({"lead_name": "Ana"}) == "Hi Ana at not given"
