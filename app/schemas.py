"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["fault", "recover"]


class ServiceIn(BaseModel):
    name: str = Field(..., min_length=1, description="Unique service name")
    dependencies: list[str] = Field(
        default_factory=list,
        description="Names of upstream services this service depends on",
    )


class GraphPayload(BaseModel):
    version: Optional[str] = Field(
        default=None, description="Optional caller-supplied graph version label"
    )
    services: list[ServiceIn] = Field(
        ..., min_length=1, description="Full dependency graph (whole-version replace)"
    )


class ReportPayload(BaseModel):
    service: str = Field(..., min_length=1)
    seq: int = Field(..., gt=0, description="Strictly positive, increasing per service")
    status: Status


class TtlPayload(BaseModel):
    ttl_seconds: int = Field(
        ...,
        gt=0,
        description="Validity window (seconds) for reports of this service, "
        "counted from the acceptance time of the last valid report",
    )


class TtlResponse(BaseModel):
    service: str
    ttl_seconds: int
    previous_ttl_seconds: Optional[int]
    in_graph: bool
    reported: bool
    expired: bool = Field(
        ..., description="True when the last valid report is already past its window"
    )
    last_status: Optional[Literal["up", "down"]]
    last_seq: Optional[int]
    accepted_at: Optional[float]


class TtlEntry(BaseModel):
    service: str
    ttl_seconds: int
    in_graph: bool
    reported: bool
    expired: Optional[bool] = Field(
        ..., description="null when the service is not in the current graph or never reported"
    )
    last_status: Optional[Literal["up", "down"]]
    last_seq: Optional[int]
    accepted_at: Optional[float]
    updated_at: Optional[str]


class TtlListResponse(BaseModel):
    configs: list[TtlEntry]


class ExpiredService(BaseModel):
    """A service whose last valid report aged out: status is now unknown."""

    service: str
    last_status: Literal["up", "down"]
    last_seq: int
    accepted_at: float
    ttl_seconds: int
    deadline: float


class RootCause(BaseModel):
    service: str
    paths: list[list[str]]


class ActionableFault(BaseModel):
    service: str
    last_seq: int


class SuppressedFault(BaseModel):
    service: str
    last_seq: int
    root_causes: list[str]
    paths: list[list[str]]


class AlertsResponse(BaseModel):
    graph_revision: int
    graph_version: Optional[str]
    services: list[str]
    actionable_faults: list[ActionableFault]
    suppressed_faults: list[SuppressedFault]
    down_services: list[str]
    unreported_services: list[str]
    healthy_services: list[str]
    expired_services: list[ExpiredService]


class GraphResponse(BaseModel):
    revision: int
    version: Optional[str]
    committed_at: Optional[str]
    services: list[ServiceIn]


class ReportResponse(BaseModel):
    applied: bool
    duplicate: bool
    service: str
    seq: int
    status: Status
    current_status: Literal["up", "down"]
    last_seq: int


class ReplaceResponse(BaseModel):
    accepted: bool
    revision: int
    version: Optional[str]
    service_count: int
    edge_count: int
    preserved_state: list[str]
