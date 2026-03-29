"""Tests for profile and report schemas."""

from datetime import datetime

from universal_debug_agent.schemas.profile import ProjectProfile
from universal_debug_agent.schemas.report import (
    DataVerification,
    Evidence,
    EvidenceType,
    ReportMetadata,
    StepStatus,
    ScenarioReport,
    ScenarioStep,
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


def test_test_report_all_pass():
    report = ScenarioReport(
        scenario_summary="购买产品 A 的完整流程",
        overall_status=StepStatus.PASS,
        steps_executed=[
            ScenarioStep(step_number=1, action="打开商品页", status=StepStatus.PASS, actual_result="页面加载成功"),
            ScenarioStep(step_number=2, action="加入购物车", status=StepStatus.PASS, actual_result="购物车+1"),
            ScenarioStep(step_number=3, action="完成支付", status=StepStatus.PASS, actual_result="显示订单成功页"),
        ],
        data_verifications=[
            DataVerification(
                check_name="订单已创建",
                query="SELECT * FROM orders WHERE user_id=1 ORDER BY id DESC LIMIT 1",
                expected="1 row with status=pending",
                actual="1 row with status=pending",
                status=StepStatus.PASS,
                severity="high",
            ),
            DataVerification(
                check_name="order_items 包含产品 A",
                query="SELECT * FROM order_items WHERE order_id=123",
                expected="product_id=A, quantity=1",
                actual="product_id=A, quantity=1",
                status=StepStatus.PASS,
                severity="high",
            ),
        ],
        evidence=[
            Evidence(type=EvidenceType.SCREENSHOT, source="/checkout/success", description="订单成功页面"),
        ],
    )

    assert report.overall_status == StepStatus.PASS
    assert len(report.steps_executed) == 3
    assert len(report.data_verifications) == 2
    assert all(s.status == StepStatus.PASS for s in report.steps_executed)
    assert all(v.status == StepStatus.PASS for v in report.data_verifications)

    # JSON roundtrip
    json_str = report.model_dump_json()
    restored = ScenarioReport.model_validate_json(json_str)
    assert restored.scenario_summary == report.scenario_summary
    assert len(restored.steps_executed) == 3


def test_test_report_with_failures():
    report = ScenarioReport(
        scenario_summary="购买产品 B",
        overall_status=StepStatus.FAIL,
        steps_executed=[
            ScenarioStep(step_number=1, action="打开商品页", status=StepStatus.PASS),
            ScenarioStep(step_number=2, action="加入购物车", status=StepStatus.FAIL, actual_result="按钮不可点击", notes="库存为0"),
        ],
        data_verifications=[
            DataVerification(
                check_name="库存检查",
                query="SELECT stock FROM products WHERE id='B'",
                expected="stock > 0",
                actual="stock = 0",
                status=StepStatus.FAIL,
                severity="high",
            ),
        ],
        issues_found=["产品 B 库存为 0，无法加入购物车"],
        next_steps=["检查库存管理逻辑", "确认测试数据是否正确"],
    )

    assert report.overall_status == StepStatus.FAIL
    assert report.steps_executed[1].status == StepStatus.FAIL
    assert len(report.issues_found) == 1


def test_test_report_blocked():
    report = ScenarioReport(
        scenario_summary="登录 + 购买",
        overall_status=StepStatus.BLOCKED,
        steps_executed=[
            ScenarioStep(
                step_number=1,
                action="登录",
                status=StepStatus.BLOCKED,
                actual_result="遇到 CAPTCHA",
                notes="无法自动解决验证码",
            ),
        ],
        issues_found=["CAPTCHA 阻止自动登录"],
    )

    assert report.overall_status == StepStatus.BLOCKED
