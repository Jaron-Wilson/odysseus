"""The Settings > Services "Sign in with ChatGPT" card (static/js/chatgptPlan.js).

The card markup is sliced out of static/index.html and the real module is
loaded, with the /api/chatgpt-plan routes answered by the test, so this pins
the shipped paste-the-URL flow: start opens the authorize URL in a new tab,
the pasted address is posted back, and the signed-in state shows the email,
the models and a Sign out that clears it.
"""
import json
import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parent.parent / "static"

AUTH_URL = "https://auth.openai.com/api/accounts/authorize?client_id=dynamic_agent_client&state=S1"


def _card_html() -> str:
    html = (_STATIC / "index.html").read_text()
    m = re.search(r'<div class="admin-card" id="cgp-card">.*?\n          </div>\n', html, re.S)
    assert m, "Sign in with ChatGPT card not found in index.html"
    return m.group(0)


def test_the_card_uses_the_settings_classes_and_links_usage():
    card = _card_html()
    assert 'class="admin-card"' in card and "admin-btn-add" in card and "admin-btn-sm" in card
    assert 'href="https://chatgpt.com/settings/usage"' in card
    assert 'id="cgp-redirect-input"' in card
    assert "\u2014" not in card                      # no em dashes in UI copy
    admin = (_STATIC / "js" / "admin.js").read_text()
    assert "import chatgptPlan from './chatgptPlan.js';" in admin
    # It sits in the Services panel, next to the other model providers.
    html = (_STATIC / "index.html").read_text()
    services = html.index('data-settings-panel="services"')
    integrations = html.index('data-settings-panel="integrations"')
    assert services < html.index('id="cgp-card"') < integrations


playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page()
        state = {"signed_in": False, "pending": False, "posts": []}
        module = (_STATIC / "js" / "chatgptPlan.js").read_text()

        def status():
            if state["signed_in"]:
                return {"signed_in": True, "email": "jaron@example.com", "plan_usage_enabled": True,
                        "redirect_uri": "http://127.0.0.1:1455/auth/callback", "pending": False}
            return {"signed_in": False, "email": "", "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                    "pending": state["pending"], "needs_sign_in_again": False}

        def route(r):
            url = r.request.url
            if url.endswith("/static/js/chatgptPlan.js"):
                return r.fulfill(body=module, content_type="text/javascript")
            if "/api/chatgpt-plan/" in url:
                path = url.split("/api/chatgpt-plan", 1)[1]
                if r.request.method == "POST":
                    state["posts"].append((path, r.request.post_data))
                if path == "/status":
                    body = status()
                elif path == "/sign-in/start":
                    state["pending"] = True
                    body = {"authorize_url": AUTH_URL, "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                            "expires_in": 600, "listening": False, "first_sign_in": True}
                elif path == "/sign-in/complete":
                    sent = json.loads(r.request.post_data or "{}")
                    if "state=S1" not in sent.get("redirect_url", ""):
                        return r.fulfill(status=400, content_type="application/json",
                                         body=json.dumps({"detail": "This sign-in link doesn't match a pending sign-in"}))
                    state["signed_in"] = True
                    state["pending"] = False
                    body = dict(status(), models=[{"slug": "gpt-5.5", "display_name": "GPT-5.5"}])
                elif path == "/models/refresh":
                    body = {"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5"}], "status": status()}
                elif path == "/sign-out":
                    state["signed_in"] = False
                    body = status()
                else:
                    return r.fulfill(status=404, body="")
                return r.fulfill(body=json.dumps(body), content_type="application/json")
            if url.rstrip("/").endswith("example.test"):
                return r.fulfill(body="<!doctype html><html><head><style>.hidden{display:none}</style></head><body>"
                                      + _card_html() + "</body></html>", content_type="text/html")
            return r.fulfill(status=404, body="")

        pg.route("**/*", route)
        pg.goto("https://example.test/")
        pg.evaluate("""() => {
          window.__opened = [];
          window.open = () => { const t = {location: {href: ''}, opener: 1, close() {}}; window.__opened.push(t); return t; };
        }""")
        pg.evaluate("() => import('/static/js/chatgptPlan.js')"
                    ".then(m => { m.init(); window.__cgp = m; return m.refreshStatus(); })")
        yield pg, state
        browser.close()


def test_paste_the_url_sign_in_then_sign_out(page):
    pg, state = page
    pg.wait_for_function("() => document.getElementById('cgp-status').textContent.includes('Not signed in')")
    assert not pg.is_visible("#cgp-signout-btn") and not pg.is_visible("#cgp-paste")

    pg.click("#cgp-signin-btn")
    pg.wait_for_selector("#cgp-paste", state="visible")
    assert pg.evaluate("window.__opened.map(t => t.location.href)") == [AUTH_URL]
    assert pg.evaluate("window.__opened[0].opener") is None
    assert pg.get_attribute("#cgp-auth-link", "href") == AUTH_URL
    assert "127.0.0.1:1455/auth/callback" in pg.text_content("#cgp-redirect-uri")

    pg.fill("#cgp-redirect-input", "http://127.0.0.1:1455/auth/callback?code=C&state=WRONG")
    pg.click("#cgp-complete-btn")
    pg.wait_for_function("() => document.getElementById('cgp-msg').textContent.includes(\"doesn't match\")")
    assert not pg.is_visible("#cgp-signout-btn")

    pasted = "http://127.0.0.1:1455/auth/callback?code=C&state=S1&client_id=oaiapp_x"
    pg.fill("#cgp-redirect-input", pasted)
    pg.press("#cgp-redirect-input", "Enter")
    pg.wait_for_function(
        "() => document.getElementById('cgp-status').textContent.includes('Signed in as jaron@example.com')")
    completes = [json.loads(body) for path, body in state["posts"] if path == "/sign-in/complete"]
    assert completes[-1] == {"redirect_url": pasted}
    assert "GPT-5.5" in pg.text_content("#cgp-models")
    assert pg.is_visible("#cgp-signout-btn") and pg.is_visible("#cgp-refresh-btn")
    assert not pg.is_visible("#cgp-paste")

    pg.click("#cgp-signout-btn")
    pg.wait_for_function("() => document.getElementById('cgp-status').textContent.includes('Not signed in')")
    assert "tokens were deleted" in pg.text_content("#cgp-msg")
    assert not pg.is_visible("#cgp-signout-btn")
