from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase


class ReportColumnIn(BaseModel):
    column_name: Annotated[str, Field(min_length=1, max_length=128)]
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    position: Annotated[int, Field(ge=0)]
    is_visible: bool = True
    data_type: Annotated[str, Field(min_length=1, max_length=32)]
    format_config: dict[str, Any] = Field(default_factory=dict)


class ReportColumnRead(ORMBase):
    id: uuid.UUID
    column_name: str
    display_name: str
    position: int
    is_visible: bool
    data_type: str
    format_config: dict[str, Any]


class ReportCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = None
    query_template_id: Annotated[str, Field(min_length=1, max_length=128)]
    parameters: dict[str, Any] = Field(default_factory=dict)
    is_public: bool = False
    is_pinned: bool = False
    columns: list[ReportColumnIn] = Field(default_factory=list)


class ReportUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parameters: dict[str, Any] | None = None
    is_public: bool | None = None
    is_pinned: bool | None = None
    columns: list[ReportColumnIn] | None = None


class ReportRead(ORMBase):
    id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    description: str | None
    query_template_id: str
    parameters: dict[str, Any]
    is_public: bool
    is_pinned: bool
    created_at: datetime
    columns: list[ReportColumnRead]


class RunReportRequest(BaseModel):
    connection_id: uuid.UUID
    overrides: dict[str, Any] = Field(default_factory=dict)


class RunReportResponse(BaseModel):
    report_id: uuid.UUID
    rows: list[dict[str, Any]]
    columns: list[ReportColumnRead]
    rows_returned: int
    execution_time_ms: int
