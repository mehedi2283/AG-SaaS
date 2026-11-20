"""Structured LLM outputs (rule 5). The model is asked for exactly this JSON (strict json_schema) and the reply is
validated here with extra="forbid". Length and range limits are checked by Pydantic after the call, so the schema
sent to OpenAI stays within what strict mode supports."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal[
    "interested", "not_interested", "question", "referral", "out_of_office", "unsubscribe", "bounce", "other"
]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailStep(_Out):
    subject: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=4000)


class LinkedInStep(_Out):
    body: str = Field(min_length=1, max_length=1900)


class ReplyClassification(_Out):
    intent: Intent
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=500)


class ReplyDraft(_Out):
    body: str = Field(min_length=1, max_length=4000)


class LeadScore(_Out):
    buyer_score: int = Field(ge=1, le=10)
    confidence: int = Field(ge=1, le=100)
    is_decision_maker: bool
    reason: str = Field(max_length=500)


class WeeklySummary(_Out):
    summary: str = Field(min_length=1, max_length=3000)


_DROP = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "title", "default"}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema in OpenAI strict form: every property required, no extras, no unsupported keywords."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in _DROP}
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    cleaned: dict[str, Any] = clean(model.model_json_schema())
    return cleaned
