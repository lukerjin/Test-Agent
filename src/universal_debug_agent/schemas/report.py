"""Investigation Report schema — structured output from the agent."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class EvidenceType(str, Enum):
    SCREENSHOT = "screenshot"
    CONSOLE_LOG = "console_log"
    NETWORK_LOG = "network_log"
    DB_QUERY = "db_query"
    CODE_SNIPPET = "code_snippet"
    CONSISTENCY_CHECK = "consistency_check"
    OTHER = "other"


class IssueClassification(str, Enum):
    FRONTEND = "frontend"
    DATA = "data"
    ENVIRONMENT = "environment"
    CONFIG = "config"
    BACKEND = "backend"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    type: EvidenceType
    source: str = ""
    description: str = ""
    content: str = ""


class ConsistencyCheck(BaseModel):
    """UI vs DB cross-validation result."""

    ui_source: str
    ui_value: str
    db_query: str
    db_value: str
    consistent: bool
    severity: str = Field(default="medium", pattern=r"^(high|medium|low)$")


class Hypothesis(BaseModel):
    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)


class ReportMetadata(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    agent_version: str = "0.1.0"
    profile_name: str = ""
    total_steps: int = 0
    mode_switches: int = 0


class InvestigationReport(BaseModel):
    """Final structured output of an investigation."""

    issue_summary: str
    steps_to_reproduce: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    consistency_checks: list[ConsistencyCheck] = Field(default_factory=list)
    root_cause_hypotheses: list[Hypothesis] = Field(default_factory=list)
    classification: IssueClassification = IssueClassification.UNKNOWN
    next_steps: list[str] = Field(default_factory=list)
    metadata: ReportMetadata = Field(default_factory=ReportMetadata)
