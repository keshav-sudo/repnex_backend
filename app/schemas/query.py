from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase

ExecStatus = Literal["success", "error", "rate_limited"]


class RunQueryRequest(BaseModel):
    session_id: uuid.UUID
    natural_language: str = Field(min_length=1, max_length=4000)


class IntentResult(BaseModel):
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str | None = None


class RunQueryResponse(BaseModel):
    history_id: uuid.UUID
    sql: str
    rows: list[dict[str, Any]]
    rows_returned: int
    execution_time_ms: int
    intent: IntentResult
    summary: str | None = None


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
