# tests/test_billing.py
import pytest

STRIPE_TEST_KEY = "sk-test-4eC39HqLyjWDarjtT1zdp7dc"


@pytest.fixture
def client():
    return StripeClient(STRIPE_TEST_KEY)
