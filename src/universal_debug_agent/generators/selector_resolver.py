"""Selector resolver — convert ARIA snapshot refs to stable Playwright locators."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ElementInfo:
    """Parsed element info from an ARIA snapshot line."""

    role: str
    name: str
    ref: str


# ARIA roles that map directly to Playwright getByRole()
_GETBYROLE_ROLES = frozenset({
    "button",
    "link",
    "textbox",
    "checkbox",
    "radio",
    "combobox",
    "option",
    "tab",
    "heading",
    "img",
    "navigation",
    "dialog",
    "alert",
    "menuitem",
    "listbox",
    "switch",
    "slider",
    "spinbutton",
    "searchbox",
    "progressbar",
    "cell",
    "row",
    "columnheader",
    "rowheader",
})

# Playwright MCP ARIA snapshots use two naming conventions:
#
# Format A (name in quotes before ref):
#   - link "About Us" [ref=e6] [cursor=pointer]:
#   - button "Submit" [ref=e32] [cursor=pointer]:
#   - heading "Title" [level=1] [ref=e85]
#
# Format B (name after colon, no quotes):
#   - listitem [ref=e4]: My Printers
#   - button [ref=e144]: Add to cart
#   - textbox [ref=e200]
#
# Format A: role "name" <attrs> [ref=id] <attrs>
_REF_PATTERN_A = re.compile(
    r'^\s*-\s+(\w+)\s+"([^"]*)"\s+(?:\[.*?\]\s*)*\[ref=(e\d+)\]'
)
# Format B: role [ref=id] <attrs>: name
_REF_PATTERN_B = re.compile(
    r"^\s*-\s+(\w+)\s+\[ref=(e\d+)\](?:\s+\[.*?\])*(?::\s*(.+?))?$"
)


def _parse_ref_line(line: str) -> ElementInfo | None:
    """Try to parse an ARIA snapshot line into an ElementInfo.

    Supports both Playwright MCP naming conventions (name-before-ref
    and name-after-colon).
    """
    # Try format A first: role "name" ... [ref=id]
    m = _REF_PATTERN_A.match(line)
    if m:
        role = m.group(1).lower()
        name = m.group(2).strip()
        ref = m.group(3)
        return ElementInfo(role=role, name=name, ref=ref)

    # Try format B: role [ref=id]: name
    m = _REF_PATTERN_B.match(line)
    if m:
        role = m.group(1).lower()
        ref = m.group(2)
        name = (m.group(3) or "").strip()
        return ElementInfo(role=role, name=name, ref=ref)

    return None


class SnapshotRefMap:
    """Maintains a ref -> ElementInfo mapping from the latest ARIA snapshot."""

    def __init__(self) -> None:
        self._map: dict[str, ElementInfo] = {}

    def update_from_snapshot(self, snapshot_text: str) -> None:
        """Parse an ARIA snapshot and rebuild the ref mapping."""
        self._map.clear()
        for line in snapshot_text.splitlines():
            info = _parse_ref_line(line)
            if info:
                self._map[info.ref] = info

    def merge_from_snapshot(self, snapshot_text: str) -> None:
        """Parse an ARIA snapshot and merge into the existing mapping.

        Unlike ``update_from_snapshot``, this does NOT clear existing entries.
        New refs overwrite, but refs not present in the new snapshot are kept.
        Useful when the auto-snapshot only covers part of the page.
        """
        for line in snapshot_text.splitlines():
            info = _parse_ref_line(line)
            if info:
                self._map[info.ref] = info

    def get(self, ref: str) -> ElementInfo | None:
        return self._map.get(ref)

    def to_locator(self, ref: str) -> str:
        """Generate a Playwright locator string for the given ref.

        Priority: getByRole > getByText > fallback comment.
        """
        info = self._map.get(ref)
        if not info:
            return f"/* WARNING: ref {ref} not resolved — use a manual locator */"

        escaped_name = info.name.replace("'", "\\'") if info.name else ""

        if info.role in _GETBYROLE_ROLES and escaped_name:
            return f"page.getByRole('{info.role}', {{ name: '{escaped_name}' }})"

        if escaped_name:
            return f"page.getByText('{escaped_name}', {{ exact: true }})"

        if info.role in _GETBYROLE_ROLES:
            return f"page.getByRole('{info.role}')"

        return f"page.locator('[role=\"{info.role}\"]')"

    def count_matching(self, role: str, name: str) -> int:
        """Count how many elements in the current snapshot share this role+name."""
        role_l = role.lower() if role else ""
        return sum(
            1
            for info in self._map.values()
            if info.role == role_l and info.name == name
        )

    def __len__(self) -> int:
        return len(self._map)


def _clean_aria_name(role: str, name: str) -> str:
    """Clean up ARIA names that make poor Playwright selectors.

    - Spinbutton/slider: name is often the current value ("1", "0.5") — drop it.
    - Textbox: name may duplicate label + required marker ("Email* Email") — deduplicate.
    - Quoted values: strip wrapping quotes from names like '"1"'.
    """
    if not name:
        return name

    # Strip wrapping quotes: "1" → 1
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1]

    # Spinbutton / slider: numeric-only names are the current value, not stable
    if role in ("spinbutton", "slider") and name.replace(".", "").replace("-", "").isdigit():
        return ""

    # Textbox: "Email* Email" → "Email"  (label + required marker duplicated)
    if role == "textbox" and "* " in name:
        parts = name.split("* ", 1)
        if len(parts) == 2 and parts[0].strip() == parts[1].strip():
            return parts[0].strip()
        # "Password* Password" → "Password"
        return parts[0].strip()

    return name


def locator_from_role_and_name(role: str, name: str) -> str:
    """Generate a Playwright locator string from an ARIA role and name.

    Unlike ``SnapshotRefMap.to_locator`` this does not need a ref lookup —
    it works directly from the role + name captured at recording time.
    """
    cleaned = _clean_aria_name(role, name)
    escaped = cleaned.replace("'", "\\'") if cleaned else ""

    if role in _GETBYROLE_ROLES and escaped:
        return f"page.getByRole('{role}', {{ name: '{escaped}' }})"

    if escaped:
        # exact: true avoids matching partial text in other elements
        return f"page.getByText('{escaped}', {{ exact: true }})"

    if role in _GETBYROLE_ROLES:
        return f"page.getByRole('{role}')"

    if role:
        return f"page.locator('[role=\"{role}\"]')"

    return "/* WARNING: no role or name — use a manual locator */"


def locator_from_dom_attrs(
    element_id: str = "",
    element_html_name: str = "",
    element_type: str = "",
    element_tag: str = "",
    element_role: str = "",
    element_name: str = "",
) -> str:
    """Build the most precise Playwright locator using real DOM attributes.

    Priority: #id > input[name] > input[type] > getByRole > getByText.
    Falls back to ARIA-based locator if no DOM attributes are available.
    """
    # 1. Best: HTML id — always unique
    if element_id:
        return f"page.locator('#{element_id}')"

    # 2. HTML name attribute — usually unique within a form
    if element_html_name and element_tag:
        return f"page.locator('{element_tag}[name=\"{element_html_name}\"]')"
    if element_html_name:
        return f"page.locator('[name=\"{element_html_name}\"]')"

    # 3. Type-specific selectors (password, number, email, etc.)
    if element_type in ("password", "number", "email", "tel", "search", "url"):
        return f"page.locator('input[type=\"{element_type}\"]')"

    # 4. Fall back to ARIA-based locator
    return locator_from_role_and_name(element_role, element_name)
