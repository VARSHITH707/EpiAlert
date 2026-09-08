"""Tests for the EpiAlert web application.

Covers authentication, server-side authorisation, and that every page renders
against the live database.

The authorisation tests matter most: hiding a link in a template is not access
control. Each protected route is requested WITHOUT a session and must be
rejected by the server itself.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from src.web.auth import create_user, hash_password, verify_password
from src.web.main import app

# Routes that require a signed-in user.
PROTECTED_PAGES = [
    "/", "/results", "/alerts", "/statistics", "/evaluation", "/sms",
]
PROTECTED_API = [
    "/api/counts", "/api/alerts", "/api/sms-summary",
]

TEST_USER = "webtest_user"
TEST_PASSWORD = "webtest_password_123"


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Client with no session cookie."""
    return TestClient(app, follow_redirects=False)


@pytest.fixture(scope="module")
def test_user() -> str:
    """Ensure a known account exists. Safe to re-run."""
    try:
        create_user(TEST_USER, TEST_PASSWORD)
    except ValueError:
        pass  # already created by an earlier run
    return TEST_USER


@pytest.fixture(scope="module")
def auth_client(test_user: str) -> TestClient:
    """Client that has signed in."""
    c = TestClient(app, follow_redirects=False)
    resp = c.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 303, "login should redirect on success"
    return c


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------

def test_password_hash_is_not_reversible():
    """The stored hash must not contain the password."""
    hashed = hash_password("correct horse battery")
    assert "correct horse battery" not in hashed
    assert hashed.startswith("$2")  # bcrypt marker


def test_password_verify_round_trip():
    hashed = hash_password("s3cret-password")
    assert verify_password("s3cret-password", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_same_password_gets_different_hashes():
    """Distinct salts, so identical passwords do not share a hash."""
    assert hash_password("same-password") != hash_password("same-password")


# ---------------------------------------------------------------------------
# Authorisation — enforced by the server, not the template
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {"accept": "text/html,application/xhtml+xml"}


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_protected_page_rejects_anonymous(client: TestClient, path: str):
    """A signed-out request must never receive page content.

    Either answer is secure: 401, or a redirect to the login form. What must
    not happen is a 200 carrying the protected page.
    """
    resp = client.get(path)
    assert resp.status_code in (401, 403, 303), (
        f"{path} served content to an unauthenticated request"
    )


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_browser_is_redirected_to_login(client: TestClient, path: str):
    """A browser gets the login form, not raw JSON."""
    resp = client.get(path, headers=BROWSER_HEADERS)
    assert resp.status_code == 303, (
        f"{path} did not redirect a browser to the login page"
    )
    assert resp.headers["location"] == "/login"


@pytest.mark.parametrize("path", PROTECTED_API)
def test_protected_api_rejects_anonymous(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code in (401, 403), (
        f"{path} served data to an unauthenticated request"
    )


@pytest.mark.parametrize("path", PROTECTED_API)
def test_api_returns_401_even_for_browser_accept(client: TestClient, path: str):
    """API paths return a status code, never a redirect.

    A script following a redirect would receive the login page HTML and might
    parse it as data. API clients need the refusal itself.
    """
    resp = client.get(path, headers=BROWSER_HEADERS)
    assert resp.status_code in (401, 403)


def test_forged_session_cookie_is_rejected(client: TestClient):
    """A cookie that is not correctly signed must not grant access."""
    c = TestClient(app, follow_redirects=False)
    c.cookies.set("epialert_session", "not-a-valid-signed-value")
    assert c.get("/").status_code in (401, 403, 303)
    # And crucially it must not receive the dashboard itself.
    assert c.get("/api/counts").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Login and logout
# ---------------------------------------------------------------------------

def test_login_page_is_public(client: TestClient):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_health_is_public(client: TestClient):
    assert client.get("/health").status_code == 200


def test_login_with_wrong_password_fails(client: TestClient, test_user: str):
    resp = client.post(
        "/login", data={"username": TEST_USER, "password": "definitely-wrong"}
    )
    assert resp.status_code == 401
    assert "epialert_session" not in resp.cookies


def test_login_with_correct_password_sets_cookie(test_user: str):
    c = TestClient(app, follow_redirects=False)
    resp = c.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 303
    assert c.cookies.get("epialert_session")


def test_logout_clears_session(auth_client: TestClient):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
    assert c.get("/").status_code == 200
    c.get("/logout")
    assert c.get("/").status_code in (401, 403, 303)


# ---------------------------------------------------------------------------
# Pages render with real data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_page_renders_when_authenticated(auth_client: TestClient, path: str):
    resp = auth_client.get(path)
    assert resp.status_code == 200
    assert "EpiAlert" in resp.text


def test_dashboard_shows_real_counts(auth_client: TestClient):
    """The dashboard must show live database counts, not placeholders."""
    resp = auth_client.get("/api/counts")
    assert resp.status_code == 200
    counts = resp.json()
    assert counts["people"] > 0
    assert counts["reports"] > 0
    assert counts["detection_results"] > 0


def test_sms_summary_proves_deduplication(auth_client: TestClient):
    """The whole point of the SMS layer: many people, few messages."""
    summary = auth_client.get("/api/sms-summary").json()
    if summary["total"] == 0:
        pytest.skip("no SMS dispatched yet")
    assert summary["max_per_alert"] <= summary["distinct_phones"], (
        "an alert sent more messages than there are distinct numbers"
    )
    assert summary["people_total"] > summary["distinct_phones"], (
        "test is meaningless unless people outnumber phone numbers"
    )


def test_alert_detail_renders(auth_client: TestClient):
    alerts = auth_client.get("/api/alerts").json()
    if not alerts:
        pytest.skip("no alerts generated yet")
    resp = auth_client.get(f"/alerts/{alerts[0]['alert_id']}")
    assert resp.status_code == 200


def test_unknown_alert_returns_404(auth_client: TestClient):
    assert auth_client.get("/alerts/99999999").status_code == 404
