"""Action log — structured record of browser actions for test code generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ActionRecord:
    """A single browser action recorded during test execution."""

    action_type: str  # navigate | click | fill | type | select | press_key | wait | dialog | db_verify

    # Navigation
    url: str = ""

    # Element interaction (resolved from ARIA snapshot)
    element_role: str = ""  # button, textbox, link, combobox, ...
    element_name: str = ""  # visible label text
    ref: str = ""  # ephemeral snapshot ref (debug only)

    # DOM attributes (resolved via browser_evaluate for precise locators)
    element_id: str = ""  # HTML id attribute
    element_html_name: str = ""  # HTML name attribute
    element_type: str = ""  # HTML type attribute (e.g. "password", "number")
    element_tag: str = ""  # HTML tag name (e.g. "input", "button", "a")
    element_class: str = ""  # CSS class list (for fallback locators)

    # Input value
    value: str = ""  # text typed/filled, option selected, key pressed

    # Page state after action
    page_url: str = ""

    # DB verification
    check_name: str = ""
    query: str = ""
    expected: str = ""
    actual: str = ""
    status: str = ""


class ActionLog:
    """Collects ActionRecords during test execution for codegen."""

    def __init__(self) -> None:
        self.records: list[ActionRecord] = []

    def record(self, action_type: str, **kwargs) -> None:
        self.records.append(ActionRecord(action_type=action_type, **kwargs))

    def to_dict_list(self) -> list[dict]:
        return [asdict(r) for r in self.records]

    def __len__(self) -> int:
        return len(self.records)
