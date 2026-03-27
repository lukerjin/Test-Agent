"""Report submission tool — allows the agent to output a structured report."""

from __future__ import annotations

import json

from agents import function_tool

from universal_debug_agent.schemas.report import (
    ConsistencyCheck,
    Evidence,
    EvidenceType,
    Hypothesis,
    InvestigationReport,
    IssueClassification,
)


@function_tool
def submit_report(report_json: str) -> str:
    """Submit a structured investigation report.

    Args:
        report_json: A JSON string with the following structure:
            {
                "issue_summary": "Brief description",
                "steps_to_reproduce": ["step 1", "step 2"],
                "evidence": [{"type": "screenshot|console_log|network_log|db_query|code_snippet|consistency_check|other", "source": "...", "description": "...", "content": "..."}],
                "consistency_checks": [{"ui_source": "...", "ui_value": "...", "db_query": "...", "db_value": "...", "consistent": true/false, "severity": "high|medium|low"}],
                "root_cause_hypotheses": [{"hypothesis": "...", "confidence": 0.0-1.0, "supporting_evidence": ["..."]}],
                "classification": "frontend|data|environment|config|backend|unknown",
                "next_steps": ["action 1", "action 2"]
            }
    """
    try:
        data = json.loads(report_json)
        report = InvestigationReport.model_validate(data)
        return report.model_dump_json(indent=2)
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON — {e}"
    except Exception as e:
        return f"Error creating report: {e}"
