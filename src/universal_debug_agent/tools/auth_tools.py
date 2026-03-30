"""Authentication helper tools for retrieving configured test credentials."""

from __future__ import annotations

import json
import os

from agents import function_tool
from pydantic import BaseModel

from universal_debug_agent.schemas.profile import TestAccount

_accounts_by_role: dict[str, dict[str, str]] = {}


class ResolvedTestAccount(BaseModel):
    role: str
    username: str
    password: str


def resolve_test_accounts(test_accounts: list[TestAccount]) -> list[ResolvedTestAccount]:
    """Resolve test account environment variables into concrete credentials."""
    resolved: list[ResolvedTestAccount] = []
    for account in test_accounts:
        username = os.environ.get(account.username_env, "")
        password = os.environ.get(account.password_env, "")
        if not username or not password:
            continue
        resolved.append(
            ResolvedTestAccount(
                role=account.role,
                username=username,
                password=password,
            )
        )
    return resolved


def configure_test_accounts(test_accounts: list[ResolvedTestAccount]) -> None:
    """Configure the test accounts exposed to the agent."""
    global _accounts_by_role
    _accounts_by_role = {
        account.role: {
            "role": account.role,
            "username": account.username,
            "password": account.password,
        }
        for account in test_accounts
    }


def _get_test_account_payload(role: str) -> str:
    """Return the configured username/password payload for a named test account role."""
    account = _accounts_by_role.get(role)
    if account is None:
        available_roles = ", ".join(sorted(_accounts_by_role))
        return (
            f"Error: no configured test account for role '{role}'. "
            f"Available roles: {available_roles or 'none'}"
        )
    return json.dumps(account, ensure_ascii=False)


@function_tool
def get_test_account(role: str) -> str:
    """Return the configured username/password for a named test account role."""
    return _get_test_account_payload(role)
