"""Shared pytest configuration for the backend test suite.

Test credentials are never hardcoded in this repository. Tests that log in as
the seeded admin/superadmin account read the password from the environment:

    TEST_ADMIN_PASSWORD   preferred; the password for the account under test
    ADMIN_PASSWORD        fallback; the same variable the server uses to seed
                          the superadmin on first startup (backend/.env)
    TEST_REGULAR_USER_PASSWORD
                          password for the pre-existing regular (non-admin)
                          test user; needed only by the regular-user tests

Example:

    BACKEND_URL=http://localhost:8000 TEST_ADMIN_PASSWORD=... \
        TEST_REGULAR_USER_PASSWORD=... pytest tests/ -v

Never print or log these values; avoid running with --showlocals (-l), which
would include module-level credential dicts in failure tracebacks.

This conftest runs before any test module is imported, so module-level
constants such as ``ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "")``
pick up the fallback value.
"""
import os
import warnings

import pytest

if not os.environ.get("TEST_ADMIN_PASSWORD") and os.environ.get("ADMIN_PASSWORD"):
    os.environ["TEST_ADMIN_PASSWORD"] = os.environ["ADMIN_PASSWORD"]


def pytest_configure(config):
    if not os.environ.get("TEST_ADMIN_PASSWORD"):
        warnings.warn(
            "TEST_ADMIN_PASSWORD (or ADMIN_PASSWORD) is not set; tests that log in "
            "as the admin account will fail authentication.",
            stacklevel=1,
        )
    if not os.environ.get("TEST_REGULAR_USER_PASSWORD"):
        warnings.warn(
            "TEST_REGULAR_USER_PASSWORD is not set; regular-user login tests will fail.",
            stacklevel=1,
        )


@pytest.fixture(scope="session")
def admin_password() -> str:
    """Password for the seeded admin account, taken from the environment."""
    pw = os.environ.get("TEST_ADMIN_PASSWORD", "")
    if not pw:
        pytest.skip("TEST_ADMIN_PASSWORD / ADMIN_PASSWORD not set")
    return pw
