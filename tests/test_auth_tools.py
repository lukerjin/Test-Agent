"""Tests for authentication helper tools."""

from unittest.mock import patch

from universal_debug_agent.schemas.profile import TestAccount
from universal_debug_agent.tools.auth_tools import (
    _get_test_account_payload,
    configure_test_accounts,
    resolve_test_accounts,
)


def test_resolve_test_accounts_reads_env_values():
    accounts = [
        TestAccount(role="user", username_env="TEST_USER_USER", password_env="TEST_USER_PASS"),
        TestAccount(role="admin", username_env="TEST_ADMIN_USER", password_env="TEST_ADMIN_PASS"),
    ]
    with patch.dict(
        "os.environ",
        {
            "TEST_USER_USER": "user@example.com",
            "TEST_USER_PASS": "secret1",
            "TEST_ADMIN_USER": "admin@example.com",
            "TEST_ADMIN_PASS": "secret2",
        },
        clear=True,
    ):
        resolved = resolve_test_accounts(accounts)

    assert len(resolved) == 2
    assert resolved[0].username == "user@example.com"
    assert resolved[1].password == "secret2"


def test_get_test_account_returns_json_credentials():
    with patch.dict(
        "os.environ",
        {
            "TEST_USER_USER": "user@example.com",
            "TEST_USER_PASS": "secret1",
        },
        clear=True,
    ):
        configure_test_accounts(
            resolve_test_accounts(
                [TestAccount(role="user", username_env="TEST_USER_USER", password_env="TEST_USER_PASS")]
            )
        )

    result = _get_test_account_payload("user")

    assert "user@example.com" in result
    assert "secret1" in result


def test_get_test_account_returns_error_for_unknown_role():
    configure_test_accounts([])
    result = _get_test_account_payload("missing")
    assert "no configured test account" in result.lower()
