"""Tests for profile and report schemas."""

from datetime import datetime

from universal_debug_agent.schemas.profile import ProjectProfile
from universal_debug_agent.schemas.report import (
    ConsistencyCheck,
    Evidence,
    EvidenceType,
    Hypothesis,
    InvestigationReport,
    IssueClassification,
    ReportMetadata,
)


def test_minimal_profile():
    data = {
        "project": {"name": "Test"},
        "code": {"root_dir": "/tmp/test"},
    }
    profile = ProjectProfile.model_validate(data)
    assert profile.project.name == "Test"
    assert profile.environment.type == "web"
    assert profile.boundaries.max_steps == 30
    assert profile.boundaries.readonly is True


def test_full_profile():
    data = {
        "project": {"name": "Full App", "description": "A full test app"},
        "environment": {
            "type": "web",
            "base_url": "https://test.example.com",
            "start_command": "npm start",
        },
        "auth": {
            "method": "form",
            "login_url": "/login",
            "test_accounts": [
                {"role": "admin", "username_env": "ADMIN_U", "password_env": "ADMIN_P"},
            ],
        },
        "code": {
            "root_dir": "/tmp/project",
            "branch": "develop",
            "entry_dirs": ["src/pages"],
            "config_files": [".env"],
        },
        "mcp_servers": {
            "playwright": {
                "enabled": True,
                "command": "npx",
                "args": ["@anthropic-ai/mcp-playwright"],
            },
            "database": {
                "enabled": False,
                "command": "node",
                "args": ["db-mcp.js"],
                "env": {"DB_HOST_ENV": "DB_HOST"},
            },
        },
        "boundaries": {
            "readonly": True,
            "max_steps": 20,
            "allowed_domains": ["test.example.com"],
        },
    }
    profile = ProjectProfile.model_validate(data)
    assert profile.project.description == "A full test app"
    assert len(profile.auth.test_accounts) == 1
    assert profile.mcp_servers["playwright"].enabled is True
    assert profile.mcp_servers["database"].enabled is False
    assert profile.boundaries.max_steps == 20


def test_investigation_report():
    report = InvestigationReport(
        issue_summary="Order status mismatch",
        steps_to_reproduce=["Login", "Go to /orders/123", "Check status"],
        evidence=[
            Evidence(
                type=EvidenceType.SCREENSHOT,
                source="/orders/123",
                description="Page shows shipped",
            ),
            Evidence(
                type=EvidenceType.DB_QUERY,
                source="orders table",
                content="status=pending",
            ),
        ],
        consistency_checks=[
            ConsistencyCheck(
                ui_source="/orders/123",
                ui_value="shipped",
                db_query="SELECT status FROM orders WHERE id=123",
                db_value="pending",
                consistent=False,
                severity="high",
            ),
        ],
        root_cause_hypotheses=[
            Hypothesis(
                hypothesis="Status mapping bug in frontend",
                confidence=0.8,
                supporting_evidence=["UI shows shipped but DB says pending"],
            ),
        ],
        classification=IssueClassification.FRONTEND,
        next_steps=["Check src/utils/statusMap.ts"],
    )

    assert report.classification == IssueClassification.FRONTEND
    assert len(report.consistency_checks) == 1
    assert report.consistency_checks[0].consistent is False

    # Test JSON serialization roundtrip
    json_str = report.model_dump_json()
    restored = InvestigationReport.model_validate_json(json_str)
    assert restored.issue_summary == report.issue_summary
    assert len(restored.root_cause_hypotheses) == 1
