from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase

ExecStatus = Literal["success", "error", "rate_limited"]


# ── Request schemas ──────────────────────────────────────────────────

class UserPersonalization(BaseModel):
    """User's personalization preferences for AI responses."""
    display_name: str = ""
    preferred_name: str = ""
    greeting_style: str = "time-based"
    ai_tone: str = "friendly"


class RunQueryRequest(BaseModel):
    session_id: uuid.UUID
    natural_language: str = Field(min_length=1, max_length=2000)


class ChatRequest(BaseModel):
    """Unified chat endpoint request."""
    natural_language: str = Field(min_length=1, max_length=2000)
    connection_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    personalization: UserPersonalization | None = None


class ExecuteRequest(BaseModel):
    """Execute a specific template with all params provided."""
    template_id: str
    params: dict[str, Any]
    connection_id: uuid.UUID
    session_id: uuid.UUID | None = None


# ── Intent schemas ───────────────────────────────────────────────────

class IntentClassification(BaseModel):
    type: Literal["conversational", "executable"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str | None = None


class MissingParam(BaseModel):
    name: str
    type: str
    description: str | None = None
    options: list[str] | None = None  # For enum types
    default: Any | None = None
    required: bool = True
    min_val: float | None = Field(None, alias="min")
    max_val: float | None = Field(None, alias="max")

    model_config = {"populate_by_name": True}


class IntentResult(BaseModel):
    template_id: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    missing_params: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str | None = None


# ── Response schemas ─────────────────────────────────────────────────

class TemplateMatch(BaseModel):
    """A template candidate returned from Pinecone search."""
    id: str
    score: float
    description: str
    module: str
    category: str


class ChatResponse(BaseModel):
    """Unified response for the chat endpoint."""
    type: Literal["conversational", "executable", "params_needed", "template_preview", "error", "access_denied"]

    message: str | None = None

    # Template info (for executable / params_needed)
    template_id: str | None = None
    template_description: str | None = None
    template_module: str | None = None

    # Extracted and missing params
    extracted_params: dict[str, Any] | None = None
    missing_params: list[MissingParam] | None = None

    # Execution results (for executable)
    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    columns: list[str] | None = None
    rows_returned: int | None = None
    execution_time_ms: int | None = None
    summary: str | None = None

    # Follow-up suggestions
    suggestions: list[str] = Field(default_factory=list)

    # Intent metadata
    intent: IntentResult | None = None
    candidates: list[TemplateMatch] | None = None


class RunQueryResponse(BaseModel):
    history_id: uuid.UUID
    sql: str
    rows: list[dict[str, Any]]
    columns: list[str] | None = None
    rows_returned: int
    execution_time_ms: int
    intent: IntentResult
    summary: str | None = None


# ── History ──────────────────────────────────────────────────────────

class QueryHistoryRead(ORMBase):
    id: uuid.UUID
    session_id: uuid.UUID
    user_id: uuid.UUID
    connection_id: uuid.UUID
    natural_language_input: str
    generated_sql: str | None
    intent: dict[str, Any]
    execution_status: ExecStatus
    error_message: str | None
    execution_time_ms: int | None
    rows_returned: int | None
    created_at: datetime
