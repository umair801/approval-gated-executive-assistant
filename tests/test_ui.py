"""Pages render, fragments work, and untrusted text is escaped."""
from __future__ import annotations

from tests.test_api import make


def test_all_pages_render():
    c, _ = make()
    for path in ["/", "/plan", "/approvals", "/audit", "/memory", "/privacy", "/docs", "/openapi.json"]:
        assert c.get(path).status_code == 200, path


def test_chat_fragment_links_to_approvals_and_resets_input():
    c, _ = make()
    r = c.post("/ui/chat", data={"message": "Plan dinner with Sam on Friday"})
    assert "Dinner plan ready" in r.text and "/approvals" in r.text
    assert 'hx-swap-oob="true"' in r.text        # clears the input box


def test_user_text_is_html_escaped():
    c, _ = make()
    r = c.post("/ui/chat", data={"message": "<script>alert(1)</script> hello"})
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_approve_through_ui_returns_done_card():
    c, ctx = make()
    c.post("/ui/chat", data={"message": "Plan dinner with Sam on Friday"})
    page = c.get("/approvals").text
    assert page.count("Approve</button>") == 2 and "(you can edit before approving)" in page
    a = next(x for x in ctx.store.list_actions("owner") if x.kind == "send_message")
    r = c.post(f"/ui/approvals/{a.action_id}/approve", data={"edited_text": "Friday 8:30, see you there"})
    assert "Done" in r.text and "simulated" in r.text and "Friday 8:30, see you there" in r.text
    assert len(ctx.store.list_outbox("owner")) == 1


def test_reject_through_ui():
    c, ctx = make()
    c.post("/ui/chat", data={"message": "Plan dinner with Sam on Friday"})
    a = ctx.store.list_actions("owner")[0]
    r = c.post(f"/ui/approvals/{a.action_id}/reject")
    assert "Nothing was done" in r.text and ctx.store.list_outbox("owner") == []


def test_double_click_on_approve_shows_a_clean_result():
    c, ctx = make()
    c.post("/ui/chat", data={"message": "Plan dinner with Sam on Friday"})
    a = ctx.store.list_actions("owner")[0]
    c.post(f"/ui/approvals/{a.action_id}/approve", data={})
    r = c.post(f"/ui/approvals/{a.action_id}/approve", data={})
    assert r.status_code == 200 and "Done" in r.text and len(ctx.store.list_outbox("owner")) == 1


def test_plan_page_shows_conflicts_and_injection_warning():
    c, _ = make()
    c.post("/ui/briefing")
    html = c.get("/plan").text
    assert "overlap" in html and "Board deck" in html
    assert "instructions aimed at an AI assistant" in html
    assert "attacker@example.invalid" not in html            # the malicious text is never shown


def test_dinner_page_lists_three_options_and_selected_one():
    c, ctx = make()
    r = c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"}).json()
    html = c.get(f"/dinner/{r['plan_id']}").text
    assert html.count("Choose this one") == 2 and "Selected" in html and "Draft message" in html


def test_audit_page_shows_chain_badge_and_filter():
    c, _ = make()
    c.post("/ui/chat", data={"message": "Plan dinner with Sam on Friday"})
    assert "Hash chain verified" in c.get("/audit").text
    assert "tool_read" in c.get("/audit").text and "tool_read" not in c.get("/audit?hide_reads=1").text.split("<tbody>")[1]


def test_memory_forms_save():
    c, ctx = make()
    c.get("/")  # seed
    c.post("/ui/memory/contact", data={"name": "Alex", "handle": "alex@example.com", "notes": "x"}, follow_redirects=False)
    c.post("/ui/memory/prefs", data={"home_area": "Harbor", "favorite_cuisines": "Thai, Korean", "max_price_level": "2",
                                     "vip_senders": "Boss"}, follow_redirects=False)
    assert any(x["name"] == "Alex" for x in ctx.store.list_contacts("owner"))
    p = ctx.store.get_prefs("owner")
    assert p["home_area"] == "Harbor" and p["favorite_cuisines"] == ["Thai", "Korean"] and p["max_price_level"] == 2
