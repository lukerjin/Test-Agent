"""Report submission tool — allows the agent to output a structured test report."""

from __future__ import annotations

import json

from agents import function_tool

from universal_debug_agent.schemas.report import ScenarioReport


@function_tool
def submit_report(report_json: str) -> str:
    """Submit a structured test report after completing (or being blocked on) a scenario.

    Args:
        report_json: A JSON string with the following structure:
            {
                "scenario_summary": "购买产品 A 的完整流程",
                "overall_status": "pass|fail|skip|blocked",
                "steps_executed": [
                    {"step_number": 1, "action": "打开商品页", "status": "pass", "actual_result": "页面正常加载", "screenshot": "", "notes": ""},
                    {"step_number": 2, "action": "加入购物车", "status": "pass", "actual_result": "购物车数量+1", "screenshot": "", "notes": ""}
                ],
                "data_verifications": [
                    {"check_name": "订单已创建", "query": "SELECT ...", "expected": "1 row", "actual": "1 row", "status": "pass", "severity": "high"},
                    {"check_name": "库存已扣减", "query": "SELECT ...", "expected": "stock=99", "actual": "stock=99", "status": "pass", "severity": "medium"}
                ],
                "evidence": [{"type": "screenshot|db_query|console_log|...", "source": "...", "description": "...", "content": "..."}],
                "issues_found": ["描述任何发现的问题"],
                "next_steps": ["建议的后续操作"]
            }
    """
    try:
        data = json.loads(report_json)
        report = ScenarioReport.model_validate(data)
        return report.model_dump_json(indent=2)
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON — {e}"
    except Exception as e:
        return f"Error creating report: {e}"
