"""Test code generator — converts action log + report into executable standalone Playwright script.

Includes a validate loop: generate → run → fix → re-run (up to MAX_FIX_ATTEMPTS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from universal_debug_agent.generators.action_log import ActionLog
from universal_debug_agent.generators.selector_resolver import (
    SnapshotRefMap,
    locator_from_dom_attrs,
    locator_from_role_and_name,
)
from universal_debug_agent.schemas.profile import ProjectProfile
from universal_debug_agent.schemas.report import ScenarioReport

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = 1
RUN_TIMEOUT_SECONDS = 120


def _locator_for(rec, ref_map: SnapshotRefMap | None = None) -> tuple[str, bool]:
    """Build a Playwright locator from an ActionRecord.

    Returns (locator_string, is_ambiguous).

    Prefers DOM attributes (id, name, type) when available for precise locators.
    Falls back to ARIA role + name when DOM attributes weren't captured.
    When the locator would match multiple elements, tries to disambiguate
    using CSS class or tag, and flags it as ambiguous.
    """
    # If we have DOM attributes, use them for a precise locator
    if rec.element_id or rec.element_html_name or rec.element_type:
        return locator_from_dom_attrs(
            element_id=rec.element_id,
            element_html_name=rec.element_html_name,
            element_type=rec.element_type,
            element_tag=rec.element_tag,
            element_role=rec.element_role,
            element_name=rec.element_name,
        ), False

    # Check if role+name is ambiguous in the current snapshot
    ambiguous = False
    if ref_map and rec.element_role and rec.element_name:
        count = ref_map.count_matching(rec.element_role, rec.element_name)
        if count > 1:
            ambiguous = True
            # Try to disambiguate using CSS class
            if rec.element_class:
                cls = rec.element_class.split()[0]  # use first class
                tag = rec.element_tag or "*"
                escaped_name = rec.element_name.replace("'", "\\'")
                return (
                    f"page.locator('{tag}.{cls}').getByText('{escaped_name}', "
                    f"{{ exact: true }})"
                ), True

    # Fall back to ARIA-based locator
    if rec.element_role or rec.element_name:
        return locator_from_role_and_name(rec.element_role, rec.element_name), ambiguous
    return "/* WARNING: no element info — use a manual locator */", False


def _build_action_summary(action_log: ActionLog, ref_map: SnapshotRefMap) -> str:
    """Convert ActionLog into a human-readable summary for the LLM prompt."""
    lines: list[str] = []
    for i, rec in enumerate(action_log.records, 1):
        if rec.action_type == "navigate":
            lines.append(f"{i}. Navigate to: {rec.url}")

        elif rec.action_type == "click":
            locator, ambiguous = _locator_for(rec, ref_map)
            line = f"{i}. Click: {rec.element_role} '{rec.element_name}' -> {locator}"
            if ambiguous:
                line += f"  ⚠️ AMBIGUOUS: multiple elements match role='{rec.element_role}' name='{rec.element_name}'"
            lines.append(line)

        elif rec.action_type in ("fill", "type"):
            locator, ambiguous = _locator_for(rec, ref_map)
            line = (
                f"{i}. Fill: {rec.element_role} '{rec.element_name}' "
                f"with '{rec.value}' -> {locator}"
            )
            if ambiguous:
                line += f"  ⚠️ AMBIGUOUS: multiple elements match role='{rec.element_role}' name='{rec.element_name}'"
            lines.append(line)

        elif rec.action_type == "select":
            locator, ambiguous = _locator_for(rec, ref_map)
            line = (
                f"{i}. Select option '{rec.value}' in {rec.element_role} "
                f"'{rec.element_name}' -> {locator}"
            )
            if ambiguous:
                line += f"  ⚠️ AMBIGUOUS: multiple elements match"
            lines.append(line)

        elif rec.action_type == "press_key":
            lines.append(f"{i}. Press key: {rec.value}")

        elif rec.action_type == "wait":
            lines.append(f"{i}. Wait: {rec.value}")

        elif rec.action_type == "dialog":
            lines.append(f"{i}. Handle dialog: {rec.value}")

        elif rec.action_type == "db_verify":
            lines.append(f"{i}. DB Check: {rec.check_name} [{rec.status}]")
            if rec.query:
                lines.append(f"   SQL: {rec.query}")
            lines.append(f"   Expected: {rec.expected}")
            lines.append(f"   Actual: {rec.actual}")

    return "\n".join(lines)


_CODEGEN_PROMPT = """\
You are a test code generator. Convert the recorded browser actions into a \
standalone Node.js script using `require('playwright')`.

## Project Context
- Base URL: {base_url}
- Scenario: {scenario_summary}

## Recorded Actions (with resolved Playwright locators)

{action_summary}

## DB Verification Results (all passed)

{db_verifications}

## Extracted Data from UI

{extracted_data}

## Auth Info
{auth_info}

## REQUIRED Script Structure

Generate a single `.js` file that follows this EXACT structure. Do NOT use \
`@playwright/test` — use `require('playwright')` directly.

```js
const {{ chromium }} = require('playwright');

// ─── Config ─────────────────────────────────────────────────────
const BASE_URL = '{base_url}';
const CREDENTIALS = {{ email: '...', password: '...' }};
const TEST_DATA = {{ /* all test-specific values here */ }};

// ─── Helpers ────────────────────────────────────────────────────
function log(step, msg) {{
  const ts = new Date().toISOString().slice(11, 19);
  console.log(`[${{ts}}] [${{step}}] ${{msg}}`);
}}

async function sleep(ms) {{
  return new Promise((r) => setTimeout(r, ms));
}}

function mysqlQuery(sql, database = 'inkstation') {{
  const {{ execSync }} = require('child_process');
  const cmd = `docker exec dockers-mysql57-1 mysql -u root -proot ${{database}} -N -e "${{sql.replace(/"/g, '\\\\"')}}" 2>/dev/null`;
  const raw = execSync(cmd, {{ encoding: 'utf-8' }}).trim();
  if (!raw) return [];
  return raw.split('\\n').map((line) => line.split('\\t'));
}}

// ─── Test Flow ──────────────────────────────────────────────────
async function run() {{
  const browser = await chromium.launch({{ headless: true }});
  const context = await browser.newContext({{ ignoreHTTPSErrors: true }});
  const page = await context.newPage();

  try {{
    // Steps go here...
    // Use log('STEP_NAME', 'message') for each step
    // Use page.waitForTimeout(N) when async behavior needs settling
    // Use page.waitForURL(...) after navigation clicks

    // ── DB Verification ─────────────────────────────────────────
    // Use mysqlQuery() with the EXACT SQL from the verification results
    // Parse returned rows and assert expected values
    // Collect errors in an array, throw at the end if any

    console.log('\\n========================================');
    console.log('  TEST: PASSED');
    console.log('========================================\\n');
  }} catch (err) {{
    console.error('\\n========================================');
    console.error('  TEST: FAILED');
    console.error('========================================');
    console.error(`  ${{err.message}}`);
    console.error('========================================\\n');
    process.exitCode = 1;
  }} finally {{
    await context.close();
    await browser.close();
  }}
}}

run();
```

## Rules

1. **TEST_DATA**: Put ALL test-specific values (emails, passwords, product IDs, \
quantities, amounts, URLs) in the `TEST_DATA` object at the top. CREDENTIALS \
gets email and password separately.

2. **Locators**: Use the resolved locators from the action summary. Prefer:
   - `page.getByRole('button', {{ name: '...' }})` for buttons
   - `page.getByRole('textbox', {{ name: '...' }})` for inputs with labels
   - `page.getByRole('link', {{ name: '...' }})` for links
   - `page.getByText('...', {{ exact: true }})` when role doesn't work
   - `page.locator('#id')` or `page.locator('.class')` as last resort
   - **⚠️ AMBIGUOUS locators**: When the action summary marks a locator as \
AMBIGUOUS, the default `getByRole`/`getByText` will match multiple elements \
and cause a strict-mode error. You MUST use a more specific selector — use the \
disambiguated locator if provided, or narrow with `.locator('.specific-class')`, \
`page.getByLabel(...)`, or `.first()` / `.nth(N)` as appropriate.

3. **Waits**: Use `await page.waitForTimeout(2000)` after clicks that trigger \
navigation or async loading. Use `await page.waitForURL(...)` after navigations. \
Use `await page.waitForSelector(...)` when waiting for specific elements.

4. **Login flow**: If the recorded actions include filling email/password and \
clicking Sign In, generate a complete login step. Use `CREDENTIALS` object.

5. **DB Verification** (CRITICAL): Convert EVERY DB check into real executable code:
   - Call `mysqlQuery(sql)` with the exact SQL from verification results
   - Parse the returned rows (each row is an array of string values)
   - Compare against expected values
   - Collect mismatches in an `errors` array
   - If `errors.length > 0`, throw with all errors joined
   - Add `await sleep(3000)` before DB queries to let queue workers finish

6. **log()**: Use `log('STEP_NAME', 'message')` at each major step for clear output.

7. **Output ONLY the JavaScript code**: No markdown fences, no explanations. \
Just valid Node.js that can be saved as a .js file and run with `node`.
"""


async def generate_test_code(
    action_log: ActionLog,
    ref_map: SnapshotRefMap,
    report: ScenarioReport,
    profile: ProjectProfile,
    model: Any = None,
) -> str | None:
    """Generate a standalone Playwright script from recorded actions.

    Returns the generated JavaScript code string, or None on failure.
    """
    if not action_log.records:
        logger.warning("[codegen] no actions recorded, skipping test generation")
        return None

    action_summary = _build_action_summary(action_log, ref_map)

    # Format DB verifications
    db_lines: list[str] = []
    for v in report.data_verifications:
        db_lines.append(f"- {v.check_name}: {v.status.value}")
        if v.query:
            db_lines.append(f"  SQL: {v.query}")
        db_lines.append(f"  Expected: {v.expected}")
        db_lines.append(f"  Actual: {v.actual}")
    db_text = "\n".join(db_lines) if db_lines else "(no DB checks)"

    # Format extracted data
    extracted = (
        json.dumps(report.extracted_data, indent=2, ensure_ascii=False)
        if report.extracted_data
        else "(none)"
    )

    # Auth info from profile
    auth_lines: list[str] = []
    if profile.auth.test_accounts:
        for acc in profile.auth.test_accounts:
            auth_lines.append(f"- Role: {acc.role}")
    auth_info = "\n".join(auth_lines) if auth_lines else "(no auth)"

    prompt = _CODEGEN_PROMPT.format(
        base_url=profile.environment.base_url,
        scenario_summary=report.scenario_summary,
        action_summary=action_summary,
        db_verifications=db_text,
        extracted_data=extracted,
        auth_info=auth_info,
    )

    client, model_name = _get_model_client(model)
    if not client or not model_name:
        logger.warning("[codegen] no model client available, skipping test generation")
        return None

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4000,
            temperature=0.2,
        )
        code = response.choices[0].message.content.strip()

        # Strip markdown fences if LLM wrapped the output
        if code.startswith("```"):
            lines = code.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)

        logger.info(f"[codegen] generated {len(code)} chars of test code")
        return code
    except Exception as e:
        logger.error(f"[codegen] LLM generation failed: {type(e).__name__}: {e}")
        return None


def _get_model_client(model: Any):
    """Extract AsyncOpenAI client and model name from the configured model."""
    from openai import AsyncOpenAI

    if model is None:
        return None, None

    # Native OpenAI: model is a plain string like "gpt-4o"
    if isinstance(model, str):
        import httpx

        return AsyncOpenAI(timeout=httpx.Timeout(30.0, connect=5.0)), model

    # OpenAIChatCompletionsModel: client in ._client, model name in .model
    client = getattr(model, "_client", None)
    model_name = getattr(model, "model", None)
    if client and model_name:
        return client, model_name

    return None, None


def save_generated_test(
    code: str,
    output_dir: str | Path,
    scenario_name: str = "test",
) -> Path:
    """Save generated test code to a .js file.

    Returns the written file path.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in scenario_name)
    file_path = out / f"{safe_name}.js"

    file_path.write_text(code, encoding="utf-8")
    logger.info(f"[codegen] saved test to {file_path}")
    return file_path


# ── Run → Fix → Re-run loop ─────────────────────────────────────


def run_generated_test(file_path: str | Path) -> tuple[bool, str, str]:
    """Run a generated .js test file with node.

    Returns (passed, stdout, stderr).
    """
    try:
        abs_path = Path(file_path).resolve()
        # Run from the project root so require('playwright') finds node_modules
        project_root = abs_path.parent
        while project_root != project_root.parent:
            if (project_root / "node_modules").exists() or (project_root / "package.json").exists():
                break
            project_root = project_root.parent
        result = subprocess.run(
            ["node", str(abs_path)],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            cwd=str(project_root),
        )
        stdout = result.stdout
        stderr = result.stderr
        passed = result.returncode == 0

        if passed:
            logger.info(f"[codegen] test PASSED: {file_path}")
        else:
            # Combine stdout + stderr for error context (some errors go to stdout)
            logger.warning(f"[codegen] test FAILED (exit={result.returncode}): {file_path}")

        return passed, stdout, stderr
    except subprocess.TimeoutExpired:
        logger.error(f"[codegen] test TIMEOUT after {RUN_TIMEOUT_SECONDS}s: {file_path}")
        return False, "", f"Test timed out after {RUN_TIMEOUT_SECONDS} seconds"
    except FileNotFoundError:
        logger.error("[codegen] node not found — cannot run generated test")
        return False, "", "node executable not found"


_FIX_PROMPT = """\
The generated Playwright test script failed. Fix it.

## Current Script

```js
{code}
```

## Error Output

```
{error_output}
```

## Rules

1. Output ONLY the fixed JavaScript code — no markdown fences, no explanations.
2. Fix the specific error shown above. Common fixes:
   - `strict mode violation: ... resolved to N elements` → use a more specific selector \
(getByRole instead of getByText, or add {{ exact: true }}, or use .nth(0), or use locator('#id'))
   - `element not found` → add waitForSelector or waitForTimeout before the action
   - `navigation timeout` → increase timeout or use waitForURL with longer timeout
   - `assertion failed` in DB check → fix the SQL or expected value comparison
3. Keep ALL other code unchanged — only fix what's broken.
4. For `strict mode violation` errors, the error message tells you the exact elements matched. \
Use that info to pick a unique selector:
   - page.getByLabel('...') for form inputs (targets the input via its label association)
   - page.locator('input[type="password"]') for password fields
   - page.locator('#id') if the error shows an id
   - page.locator('button.specific-class') to narrow by CSS class
   - page.getByRole('button', {{ name: '...' }}).nth(N) as last resort
"""


async def _fix_test_code(code: str, error_output: str, model: Any) -> str | None:
    """Send the failing code + error to LLM for a fix.

    Returns the fixed code string, or None on failure.
    """
    client, model_name = _get_model_client(model)
    if not client or not model_name:
        return None

    prompt = _FIX_PROMPT.format(code=code, error_output=error_output[-3000:])

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4000,
            temperature=0.2,
        )
        fixed = response.choices[0].message.content.strip()

        # Strip markdown fences
        if fixed.startswith("```"):
            lines = fixed.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            fixed = "\n".join(lines)

        logger.info(f"[codegen] LLM produced fix ({len(fixed)} chars)")
        return fixed
    except Exception as e:
        logger.error(f"[codegen] fix LLM call failed: {type(e).__name__}: {e}")
        return None


async def generate_and_validate(
    action_log: ActionLog,
    ref_map: SnapshotRefMap,
    report: ScenarioReport,
    profile: ProjectProfile,
    model: Any = None,
    output_dir: str | Path = "artifacts/generated_tests",
    scenario_name: str = "test",
) -> tuple[Path | None, bool]:
    """Generate test code, run it, fix on failure, re-run. Up to MAX_FIX_ATTEMPTS.

    Returns (file_path, passed). file_path is None if generation failed entirely.
    """
    # Step 1: Generate initial code
    code = await generate_test_code(
        action_log=action_log,
        ref_map=ref_map,
        report=report,
        profile=profile,
        model=model,
    )
    if not code:
        return None, False

    file_path = save_generated_test(code, output_dir, scenario_name)

    # Step 2: Run → check → fix loop
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        logger.info(f"[codegen] validation attempt {attempt}/{MAX_FIX_ATTEMPTS}")

        passed, stdout, stderr = await asyncio.to_thread(
            run_generated_test, file_path
        )

        if passed:
            logger.info(f"[codegen] test PASSED on attempt {attempt}")
            return file_path, True

        if attempt >= MAX_FIX_ATTEMPTS:
            logger.warning(
                f"[codegen] test still failing after {MAX_FIX_ATTEMPTS} attempts, giving up"
            )
            break

        # Combine stdout + stderr for error context
        error_output = ""
        if stderr:
            error_output += stderr
        if stdout:
            error_output += "\n" + stdout

        logger.info(f"[codegen] attempt {attempt} failed, asking LLM to fix...\n--- error output ---\n{error_output[:2000]}\n--- end ---")
        fixed_code = await _fix_test_code(code, error_output, model)
        if not fixed_code:
            logger.warning("[codegen] LLM fix failed, stopping")
            break

        # Save the fixed version and loop
        code = fixed_code
        file_path.write_text(code, encoding="utf-8")
        logger.info(f"[codegen] saved fix to {file_path}")

    return file_path, False


# ── Claude Code CLI variants ────────────────────────────────────



def _build_steps_summary(report: ScenarioReport) -> str:
    """Build an action summary from report steps (for CLI mode without action_log)."""
    lines: list[str] = []
    for s in report.steps_executed:
        lines.append(f"{s.step_number}. [{s.status.value}] {s.action}")
        if s.notes:
            # notes contains locator, context, url_after from CLI execution
            for part in s.notes.split(" | "):
                lines.append(f"   {part}")
        if s.actual_result:
            lines.append(f"   Result: {s.actual_result}")
    return "\n".join(lines) if lines else "(no steps recorded)"


def _build_codegen_prompt(
    report: ScenarioReport,
    profile: ProjectProfile,
    file_path: Path,
    scenario: str = "",
) -> str:
    """Build the prompt for CLI codegen — instructs Claude to write the file directly."""
    action_summary = _build_steps_summary(report)

    db_lines: list[str] = []
    for v in report.data_verifications:
        db_lines.append(f"- {v.check_name}: {v.status.value}")
        if v.query:
            db_lines.append(f"  SQL: {v.query}")
        db_lines.append(f"  Expected: {v.expected}")
        db_lines.append(f"  Actual: {v.actual}")
    db_text = "\n".join(db_lines) if db_lines else "(no DB checks)"

    extracted = (
        json.dumps(report.extracted_data, indent=2, ensure_ascii=False)
        if report.extracted_data
        else "(none)"
    )

    auth_lines: list[str] = []
    if profile.auth.login_url:
        auth_lines.append(f"- Login URL: {profile.auth.login_url}")
        auth_lines.append(f"- Method: {profile.auth.method}")
    if profile.auth.test_accounts:
        for acc in profile.auth.test_accounts:
            username = os.environ.get(acc.username_env, acc.username_env)
            password = os.environ.get(acc.password_env, acc.password_env)
            auth_lines.append(f"- Account ({acc.role}): email={username}, password={password}")
    if not auth_lines:
        auth_lines.append("(no auth)")
    auth_lines.append("")
    auth_lines.append(
        "IMPORTANT: The generated test runs in a fresh browser with NO session. "
        "The test MUST log in first before proceeding with the scenario steps."
    )
    auth_info = "\n".join(auth_lines)

    scenario_text = scenario or report.scenario_summary

    base_prompt = _CODEGEN_PROMPT.format(
        base_url=profile.environment.base_url,
        scenario_summary=scenario_text,
        action_summary=action_summary,
        db_verifications=db_text,
        extracted_data=extracted,
        auth_info=auth_info,
    )

    return (
        f"{base_prompt}\n\n"
        f"IMPORTANT: Write the generated code to this file: {file_path.resolve()}\n"
        f"Use the Write tool to save the complete .js file."
    )


async def generate_and_validate_cli(
    report: ScenarioReport,
    profile: ProjectProfile,
    output_dir: str | Path = "artifacts/generated_tests",
    scenario_name: str = "test",
    scenario: str = "",
) -> tuple[Path | None, bool]:
    """CLI codegen: let Claude write the file directly, then validate by running it."""
    from universal_debug_agent.orchestrator.claude_executor import _run_claude_cli

    if not report.steps_executed:
        logger.warning("[codegen-cli] no steps in report, skipping")
        return None, False

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in scenario_name)
    file_path = out / f"{safe_name}.js"

    # Step 1: Generate — Claude writes the file via its tools
    prompt = _build_codegen_prompt(report, profile, file_path, scenario)
    try:
        _, _, rc = await _run_claude_cli(
            prompt, timeout_seconds=300, label="codegen",
        )
    except Exception as e:
        logger.error(f"[codegen-cli] generation failed: {type(e).__name__}: {e!r}")
        return None, False

    if not file_path.exists():
        logger.warning(f"[codegen-cli] Claude did not create {file_path}")
        return None, False

    logger.info(f"[codegen-cli] generated {file_path.stat().st_size} bytes -> {file_path}")

    # Step 2: Run → fix loop
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        logger.info(f"[codegen-cli] validation attempt {attempt}/{MAX_FIX_ATTEMPTS}")

        passed, stdout, stderr = await asyncio.to_thread(
            run_generated_test, file_path
        )

        if passed:
            logger.info(f"[codegen-cli] test PASSED on attempt {attempt}")
            return file_path, True

        error_output = ""
        if stderr:
            error_output += stderr
        if stdout:
            error_output += "\n" + stdout

        logger.info(f"[codegen-cli] attempt {attempt} failed, error:\n{error_output[:2000]}")

        if attempt >= MAX_FIX_ATTEMPTS:
            logger.warning(
                f"[codegen-cli] still failing after {MAX_FIX_ATTEMPTS} attempts"
            )
            break

        # Fix — tell Claude to read the file, fix it, save it back
        fix_prompt = (
            f"The test at {file_path.resolve()} is failing. Fix it.\n\n"
            f"## Error Output\n```\n{error_output[-3000:]}\n```\n\n"
            f"Read the file, fix the specific error, and save the corrected version.\n"
            f"Common fixes:\n"
            f"- strict mode violation → use a more specific selector\n"
            f"- element not found → add waitForSelector or waitForTimeout\n"
            f"- navigation timeout → increase timeout\n"
            f"- assertion failed → fix the SQL or comparison\n"
        )
        try:
            _, _, fix_rc = await _run_claude_cli(
                fix_prompt, timeout_seconds=300, label="codegen-fix",
            )
        except Exception as e:
            logger.error(f"[codegen-cli] fix failed: {type(e).__name__}: {e!r}")
            break

        if not file_path.exists():
            logger.warning("[codegen-cli] file disappeared after fix")
            break

        logger.info(f"[codegen-cli] fix applied ({file_path.stat().st_size} bytes)")

    return file_path, False
