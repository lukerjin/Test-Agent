"""Test Report schema — structured output from the agent."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    BLOCKED = "blocked"


class EvidenceType(str, Enum):
    SCREENSHOT = "screenshot"
    CONSOLE_LOG = "console_log"
    NETWORK_LOG = "network_log"
    DB_QUERY = "db_query"
    CODE_SNIPPET = "code_snippet"
    OTHER = "other"


class Evidence(BaseModel):
    type: EvidenceType
    source: str = ""
    description: str = ""
    content: str = ""


class ScenarioStep(BaseModel):
    """A single executed step in the test scenario."""

    step_number: int
    action: str  # "打开商品页", "点击加入购物车", "填写地址"
    status: StepStatus = StepStatus.PASS
    actual_result: str = ""  # what actually happened
    screenshot: str = ""  # screenshot path/reference if taken
    notes: str = ""  # e.g. "检测到未登录，自动走登录流程"


class DataVerification(BaseModel):
    """A single data verification check — UI vs DB or expected vs actual."""

    check_name: str  # "订单是否创建", "库存是否扣减"
    query: str = ""  # SQL or description of how data was checked
    expected: str = ""  # what we expected
    actual: str = ""  # what we found
    status: StepStatus = StepStatus.PASS
    severity: str = Field(default="medium", pattern=r"^(high|medium|low)$")


class ReportMetadata(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    agent_version: str = "0.1.0"
    profile_name: str = ""
    total_steps: int = 0
    mode_switches: int = 0


class ScenarioReport(BaseModel):
    """Final structured output of a test scenario execution."""

    scenario_summary: str = Field(
        description="One short sentence (≤80 chars): what scenario was tested. E.g. 'Checkout via bank transfer for p-16227'"
    )
    overall_status: StepStatus = StepStatus.PASS  # pass only if ALL steps + verifications pass
    steps_executed: list[ScenarioStep] = Field(default_factory=list)
    extracted_data: dict[str, str | int | bool | None] = Field(
        default_factory=dict,
        description=(
            "Key business values visible on the UI at completion: order IDs, amounts, "
            "statuses, user IDs, etc. Extracted from the final confirmation/success page. "
            "Used as input for DB verification. E.g. {'order_id': '1234', 'total': '268.45'}"
        ),
    )
    data_verifications: list[DataVerification] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    issues_found: list[str] = Field(default_factory=list)  # any problems encountered — first item is the key blocker (≤80 chars)
    next_steps: list[str] = Field(default_factory=list)  # recommendations
    metadata: ReportMetadata = Field(default_factory=ReportMetadata)
