#!/usr/bin/env python3
"""Fetch all table schemas from the DB MCP server and save to cache.

Usage:
    uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml
    uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml --database inkstation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from universal_debug_agent.mcp.factory import create_mcp_server
from universal_debug_agent.schemas.profile import ProjectProfile
from universal_debug_agent.tools.db_tool import _parse_describe_result

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _extract_text(result) -> str:
    """Extract text from MCP CallToolResult, handling both dict and Pydantic objects."""
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if hasattr(content, "text"):
        return content.text
    return str(content)


async def fetch_all_schemas(
    profile: ProjectProfile,
    target_databases: list[str] | None = None,
    skip_databases: set[str] | None = None,
    existing_cache: dict[str, str] | None = None,
) -> dict[str, str]:
    """Connect to DB MCP server, list all tables, describe each one."""

    skip_databases = skip_databases or {
        "information_schema", "mysql", "performance_schema", "sys",
    }

    # Find the database MCP server config
    db_config = None
    db_name = None
    for name, cfg in profile.mcp_servers.items():
        if cfg.role == "database" or "database" in name.lower():
            db_config = cfg
            db_name = name
            break

    if not db_config:
        logger.error("No database MCP server found in profile")
        return {}

    server = create_mcp_server(db_name, db_config)
    logger.info(f"Connecting to MCP server '{db_name}'...")
    await server.connect()

    try:
        # Step 1: List databases
        result = await server.call_tool("list_databases", {})
        raw = _extract_text(result)
        try:
            db_info = json.loads(raw)
            all_databases = db_info.get("databases", [])
        except json.JSONDecodeError:
            all_databases = [line.strip() for line in raw.splitlines() if line.strip()]
        logger.info(f"Found {len(all_databases)} databases")

        if target_databases:
            databases = [d for d in all_databases if d in target_databases]
        else:
            databases = [d for d in all_databases if d not in skip_databases]

        logger.info(f"Will scan: {', '.join(databases)}")

        cache: dict[str, str] = {}

        for db in databases:
            # Try describe_all_tables first (1 call for all tables)
            try:
                logger.info(f"  [{db}] calling describe_all_tables...")
                result = await server.call_tool("describe_all_tables", {"database": db})
                raw = _extract_text(result)

                if raw and "Error" not in raw:
                    tables_data = json.loads(raw)
                    for table_name, columns in tables_data.items():
                        if existing_cache and f"{db}.{table_name}" in existing_cache:
                            continue
                        if isinstance(columns, list):
                            # Same format as describe_table: list of {Field, Type, Key, Extra, ...}
                            parsed = _parse_describe_result(json.dumps(columns))
                        elif isinstance(columns, str):
                            parsed = columns
                        else:
                            parsed = str(columns)[:500]
                        cache[f"{db}.{table_name}"] = parsed
                    logger.info(f"  [{db}] cached {sum(1 for k in cache if k.startswith(db + '.'))} tables via describe_all_tables")
                    continue
                else:
                    logger.warning(f"  [{db}] describe_all_tables failed, falling back to per-table: {raw[:100]}")
            except Exception as e:
                logger.warning(f"  [{db}] describe_all_tables not available ({e}), falling back to per-table")

            # Fallback: list_tables + describe_table one by one
            result = await server.call_tool("list_tables", {"database": db})
            raw = _extract_text(result)

            if not raw or "Error" in raw:
                logger.warning(f"  [{db}] skipped: {raw[:100]}")
                continue

            tables = [t.strip() for t in raw.strip().splitlines() if t.strip()]
            logger.info(f"  [{db}] {len(tables)} tables (per-table fallback)")

            if existing_cache:
                tables = [t for t in tables if f"{db}.{t}" not in existing_cache]
                logger.info(f"  [{db}] {len(tables)} tables to fetch (skipping cached)")

            for i, table in enumerate(tables):
                try:
                    result = await server.call_tool(
                        "describe_table",
                        {"database": db, "table": table},
                    )
                    raw = _extract_text(result)

                    if not raw or "Error" in raw:
                        logger.warning(f"    {db}.{table}: {raw[:80]}")
                        continue

                    parsed = _parse_describe_result(raw)
                    cache[f"{db}.{table}"] = parsed

                    if (i + 1) % 50 == 0:
                        logger.info(f"    ... {i + 1}/{len(tables)} done")

                    if (i + 1) % 10 == 0:
                        await asyncio.sleep(0.5)

                except Exception as e:
                    logger.warning(f"    {db}.{table}: {e}")
                    if "Connection lost" in str(e) or "closed" in str(e):
                        logger.info(f"    Connection lost. Restarting MCP server...")
                        try:
                            await server.cleanup()
                        except Exception:
                            pass
                        await asyncio.sleep(5)
                        server = create_mcp_server(db_name, db_config)
                        await server.connect()
                        await asyncio.sleep(2)

            logger.info(f"  [{db}] cached {sum(1 for k in cache if k.startswith(db + '.'))} tables")

        return cache

    finally:
        await server.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Cache all DB table schemas")
    parser.add_argument("-p", "--profile", required=True, help="Path to project profile YAML")
    parser.add_argument("-d", "--database", action="append", help="Only scan these databases (repeatable)")
    parser.add_argument("-o", "--output", help="Output path (default: memory/db_schema_<project>.json)")
    parser.add_argument("--skip-cached", action="store_true", help="Skip tables already in cache")
    args = parser.parse_args()

    import yaml
    from dotenv import load_dotenv
    load_dotenv()

    profile_path = Path(args.profile)
    profile_data = yaml.safe_load(profile_path.read_text())
    profile = ProjectProfile.model_validate(profile_data)

    # Load existing cache for --skip-cached
    output_path_early = args.output or f"memory/db_schema_{profile.project.name.replace(' ', '_')}.json"
    existing_cache = {}
    if args.skip_cached and Path(output_path_early).exists():
        try:
            existing_cache = json.loads(Path(output_path_early).read_text())
            logger.info(f"Loaded {len(existing_cache)} cached schemas")
        except Exception:
            pass

    cache = asyncio.run(fetch_all_schemas(profile, target_databases=args.database, existing_cache=existing_cache))

    output_path = args.output or f"memory/db_schema_{profile.project.name.replace(' ', '_')}.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Merge with existing cache (keep previously cached tables)
    existing = {}
    if Path(output_path).exists():
        try:
            existing = json.loads(Path(output_path).read_text())
        except Exception:
            pass
    merged = {**existing, **cache}
    Path(output_path).write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    new_count = len(merged) - len(existing)
    logger.info(f"\nSaved {len(merged)} table schemas to {output_path} ({new_count} new)")

    logger.info(f"\nSaved {len(cache)} table schemas to {output_path}")


if __name__ == "__main__":
    main()
