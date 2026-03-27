"""Tests for StuckDetector and EvidenceCollector."""

from universal_debug_agent.orchestrator.state_machine import (
    EvidenceCollector,
    StuckDetector,
)


class TestStuckDetector:
    def test_not_stuck_initially(self):
        sd = StuckDetector(max_steps=30)
        assert sd.is_stuck() is False

    def test_repeated_calls(self):
        sd = StuckDetector(max_steps=30)
        for _ in range(3):
            sd.record("read_file", "path=src/app.ts")
            sd.update_last_result("abc123")

        assert sd.is_stuck() is True
        assert "Repeated identical tool call" in sd.stuck_reason()

    def test_different_calls_not_stuck(self):
        sd = StuckDetector(max_steps=30)
        sd.record("read_file", "path=a.ts")
        sd.update_last_result("aaa")
        sd.record("grep_code", "pattern=login")
        sd.update_last_result("bbb")
        sd.record("read_file", "path=b.ts")
        sd.update_last_result("ccc")

        assert sd.is_stuck() is False

    def test_same_results(self):
        sd = StuckDetector(max_steps=30)
        for i in range(5):
            sd.record(f"tool_{i}", f"args_{i}")
            sd.update_last_result("same_hash")

        assert sd.is_stuck() is True
        assert "identical results" in sd.stuck_reason()

    def test_budget_exceeded(self):
        sd = StuckDetector(max_steps=10)
        # 70% of 10 = 7, so after 8 steps without report -> stuck
        for i in range(8):
            sd.record(f"tool_{i}", f"args_{i}")
            sd.update_last_result(f"hash_{i}")

        assert sd.is_stuck() is True
        assert "without submitting a report" in sd.stuck_reason()

    def test_budget_ok_with_report(self):
        sd = StuckDetector(max_steps=10)
        for i in range(7):
            sd.record(f"tool_{i}", f"args_{i}")
            sd.update_last_result(f"hash_{i}")

        # Submit a report — should not trigger budget rule
        sd.record("submit_report", "{}")
        sd.update_last_result("report_hash")

        assert sd.is_stuck() is False

    def test_step_count(self):
        sd = StuckDetector(max_steps=30)
        assert sd.step_count == 0
        sd.record("a", "b")
        sd.record("c", "d")
        assert sd.step_count == 2


class TestEvidenceCollector:
    def test_collect_and_summary(self):
        ec = EvidenceCollector()
        ec.collect("read_file", "path=app.ts", "function main() { ... }")
        ec.collect("grep_code", "pattern=login", "app.ts:10:login()")

        summary = ec.build_summary()
        assert "Evidence #1: read_file" in summary
        assert "Evidence #2: grep_code" in summary
        assert "app.ts:10:login()" in summary

    def test_empty_summary(self):
        ec = EvidenceCollector()
        assert ec.build_summary() == "No evidence collected."

    def test_long_result_truncated(self):
        ec = EvidenceCollector()
        long_result = "x" * 1000
        ec.collect("tool", "args", long_result)
        assert len(ec.items[0]["result_preview"]) == 500
