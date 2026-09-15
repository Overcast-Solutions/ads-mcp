"""Locked oracle conftest: path bootstrap + shared fixtures.

The tests directory is import-locked after the oracle commit (shipwright
hard rule 2). Contract notes binding on the implementation live in
``tests/harness.py``.
"""

import sys
from pathlib import Path

# Make `import harness` / `import tool_catalog` deterministic under any
# pytest import mode.
_TESTS_DIR = str(Path(__file__).parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

import harness


@pytest.fixture
def fake_client():
    """A bare recorded-fixture GoogleAdsClient stand-in."""
    return harness.FakeGoogleAdsClient()


@pytest.fixture
def account_client():
    """Fake client pre-loaded with the standard fixture account."""
    return harness.stub_standard_account(harness.FakeGoogleAdsClient())


@pytest.fixture
def clock():
    return harness.FakeClock()
