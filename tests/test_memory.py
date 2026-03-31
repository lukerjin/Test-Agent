"""Tests for memory store."""

import tempfile
from pathlib import Path

from universal_debug_agent.memory.store import (
    MemoryRecord,
    MemoryStore,
    resolve_memory_path,
)


class TestMemoryStore:
    def test_empty_store(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "test.jsonl")
            assert store.load() == []
            assert store.build_prompt_context() == ""

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "test.jsonl")

            store.save(MemoryRecord(
                issue="Login page broken",
                root_cause="Session cookie not set",
                classification="frontend",
                key_findings=["Cookie header missing in response"],
            ))

            records = store.load()
            assert len(records) == 1
            assert records[0].issue == "Login page broken"
            assert records[0].root_cause == "Session cookie not set"

    def test_append_multiple(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "test.jsonl")

            store.save(MemoryRecord(issue="Issue 1", classification="frontend"))
            store.save(MemoryRecord(issue="Issue 2", classification="data"))
            store.save(MemoryRecord(issue="Issue 3", classification="config"))

            records = store.load()
            assert len(records) == 3
            assert records[0].issue == "Issue 1"
            assert records[2].issue == "Issue 3"

    def test_build_prompt_context(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "test.jsonl")

            store.save(MemoryRecord(
                issue="Order status wrong",
                root_cause="status_map bug",
                classification="frontend",
                key_findings=["status_map.ts line 42"],
            ))

            ctx = store.build_prompt_context()
            assert "Past Run Lessons" in ctx
            assert "Order status wrong" in ctx
            assert "status_map bug" in ctx

    def test_prompt_context_max_entries(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "test.jsonl")

            for i in range(10):
                store.save(MemoryRecord(issue=f"Issue {i}", classification="unknown"))

            # Only last 3
            ctx = store.build_prompt_context(max_entries=3)
            assert "Issue 7" in ctx
            assert "Issue 8" in ctx
            assert "Issue 9" in ctx
            assert "Issue 0" not in ctx

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "nested" / "dir" / "test.jsonl")
            store.save(MemoryRecord(issue="test"))
            assert store.path.exists()
            assert len(store.load()) == 1

    def test_skips_invalid_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.jsonl"
            path.write_text(
                '{"issue": "valid record", "classification": "frontend"}\n'
                'this is not json\n'
                '{"issue": "another valid", "classification": "data"}\n'
            )
            store = MemoryStore(path)
            records = store.load()
            assert len(records) == 2
            assert records[0].issue == "valid record"
            assert records[1].issue == "another valid"


class TestResolveMemoryPath:
    def test_basic(self):
        result = resolve_memory_path("./memory/{project_name}.jsonl", "My Web App")
        assert result == "./memory/my_web_app.jsonl"

    def test_no_placeholder(self):
        result = resolve_memory_path("./fixed_path.jsonl", "anything")
        assert result == "./fixed_path.jsonl"

    def test_special_chars(self):
        result = resolve_memory_path("./{project_name}.jsonl", "org/repo-name")
        assert result == "./org_repo-name.jsonl"
