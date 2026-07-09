from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ─── Client → Server ───────────────────────────────────────────────────────


class RunQueryMsg(BaseModel):
    action: Literal["run_query"]
    natural_language: str = Field(min_length=1, max_length=4000)


class CancelMsg(BaseModel):
    action: Literal["cancel"]


class PingMsg(BaseModel):
    action: Literal["ping"]


class PauseMsg(BaseModel):
    action: Literal["pause"]


class ResumeMsg(BaseModel):
    action: Literal["resume"]


WSClientMessage = Annotated[
    RunQueryMsg | CancelMsg | PingMsg | PauseMsg | ResumeMsg, Field(discriminator="action")
]


# ─── Server → Client ───────────────────────────────────────────────────────


class StatusMsg(BaseModel):
    type: Literal["status"] = "status"
    message: str


class ProgressMsg(BaseModel):
    type: Literal["progress"] = "progress"
    step: Literal["intent_extraction", "sql_build", "execute", "insight"]


class SqlMsg(BaseModel):
    type: Literal["sql"] = "sql"
    sql: str


class DataMsg(BaseModel):
    type: Literal["data"] = "data"
    batch: int
    rows: list[dict[str, Any]]


class InsightMsg(BaseModel):
    type: Literal["insight"] = "insight"
    summary: str


class CompleteMsg(BaseModel):
    type: Literal["complete"] = "complete"
    history_id: str
    rows_returned: int
    exec_time_ms: int
    columns: list[str] | None = None



class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    history_id: str | None = None


class ReadyMsg(BaseModel):
    type: Literal["ready"] = "ready"
    session_id: str


class PongMsg(BaseModel):
    type: Literal["pong"] = "pong"
