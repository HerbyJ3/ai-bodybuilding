"""Template-level checks for the client app and the coach's support card (BUILD_SPEC §15):
the right forms post to the right routes, plain-language wording, no date on the check-in,
untrusted text escaped. Synthetic client only, fake LLM, no network."""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from approvals.queue import ApprovalQueue
from clientapp.app import create_client_app
from dashboard.app import create_app
from dashboard.store import ChatStore, SupportStore
from ingest.onboarding import load_onboarding_config, onboard
from store.event_store import EventStore
from tests.test_dashboard import AS_OF, CID, FakeLLM
from tests.test_m6_import_onboarding import SAMPLE

TODAY = date(2026, 9, 28)


@pytest.fixture()
def env(tmp_path, cfg):
    data = tmp_path / "data"
    folder = data / "syn"
    folder.mkdir(parents=True)
    onboard(EventStore(folder / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    llm = FakeLLM()
    client_app = TestClient(create_client_app(data, cfg, llm_factory=lambda: llm, today=lambda: TODAY))
    admin = TestClient(create_app(data, cfg, llm_factory=lambda: llm))
    return client_app, admin, folder / "coach.db"


def form(html: str, action: str) -> str:
    assert f'action="{action}"' in html, action
    return html.split(f'action="{action}"')[1].split("</form>")[0]


def section(html: str, sid: str) -> str:
    return html.split(f'id="{sid}"')[1].split("</section>")[0]


# --- client app ------------------------------------------------------------------------

def test_every_client_form_posts_to_its_route(env):
    client, _, _ = env
    html = client.get(f"/c/{CID}").text
    for path, fields in (("weigh-in", ("weight", "unit", "conditions", "when")),
                         ("daily-target", ("hit", "off_protein", "off_carbs", "off_fat", "non_training", "when")),
                         ("checkin", ("hunger", "energy", "training_feel", "sleep_hours", "notes")),
                         ("chat", ("message",)), ("support", ("message",))):
        f = form(html, f"/c/{CID}/{path}")
        for name in fields:
            assert f'name="{name}"' in f, (path, name)
    assert html.count('method="post"') == 5


def test_daily_target_has_non_training_checkbox_and_plain_question(env):
    client, _, _ = env
    html = client.get(f"/c/{CID}").text
    f = form(html, f"/c/{CID}/daily-target")
    assert 'type="checkbox" name="non_training"' in f and "disabled" not in f.split('name="non_training"')[1][:40]
    assert 'type="radio" name="hit" value="yes"' in f and 'type="radio" name="hit" value="no"' in f
    assert "Did you hit your targets today?" in html
    assert "50 g under" in f and "50 g over" in f  # off-by choices in words


def test_checkin_has_dropdowns_and_no_date(env):
    client, _, _ = env
    f = form(client.get(f"/c/{CID}").text, f"/c/{CID}/checkin")
    assert 'type="date"' not in f and 'name="when"' not in f
    for name in ("hunger", "energy", "training_feel", "sleep_hours"):
        assert f'<select name="{name}"' in f
    assert f.count('class="clear"') == 5  # red x on each dropdown + notes


def test_section_order_and_nav(env):
    client, _, _ = env
    html = client.get(f"/c/{CID}").text
    order = [html.index(f'<section class="card{c}" id="{s}"') for c, s in
             (("", "weigh-in"), ("", "daily-target"), ("", "checkin"), ("", "targets"), ("", "chart"),
              (" chat", "chat"), (" chat", "support"))]
    assert order == sorted(order)
    nav = html.split('class="section-nav"')[1].split("</nav>")[0]
    for anchor in ("#today", "#targets", "#chart", "#chat", "#support"):
        assert f'href="{anchor}"' in nav
    assert "Your coach can see this chat" in section(html, "chat")
    assert ("For questions about your plan, ask Mr. J. Use this for anything else you need your coach for."
            in section(html, "support"))


def test_targets_in_plain_words(env, cfg):
    client, _, db = env
    q = ApprovalQueue(EventStore(db), cfg)
    q.refresh(CID, TODAY)
    pid = q.pending(CID)[0].proposal.proposal_id
    q.decide(pid, "approved", at=datetime(2026, 9, 28, 9, tzinfo=timezone.utc))
    html = section(client.get(f"/c/{CID}").text, "targets")
    assert "Rest day" in html and "Training day (moderate)" in html and "non_training" not in html
    assert "On Sep 28, 2026 your coach approved:" in html
    assert "decrease_calories" not in html and "calories.cut" not in html


def test_chart_has_legend_tooltip_and_table(env):
    client, _, _ = env
    html = section(client.get(f"/c/{CID}").text, "chart")
    assert 'class="legend"' in html and 'class="tooltip"' in html and "Table view" in html


def test_banners_and_escaping(env):
    client, _, db = env
    SupportStore(EventStore(db)).add(CID, "coach", "<script>alert(1)</script>")
    html = client.get(f"/c/{CID}", params={"notice": "Saved <b>", "error": "Bad <i>"}).text
    assert 'role="status">Saved &lt;b&gt;' in html and 'role="alert">Bad &lt;i&gt;' in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_pick_page_lists_clients(env):
    client, _, _ = env
    html = client.get("/").text
    assert "Who's checking in?" in html and f'href="/c/{CID}"' in html


def test_client_static_js_served(env):
    client, _, _ = env
    assert client.get("/app-static/client.js").status_code == 200


# --- coach/admin view ------------------------------------------------------------------

def test_admin_support_card_forms_and_client_chat(env):
    _, admin, db = env
    SupportStore(EventStore(db)).add(CID, "client", "SYNTH-SUPPORT-Q")
    ChatStore(EventStore(db), "client").add(CID, "user", "SYNTH-CLIENT-CHAT")
    html = admin.get(f"/client/{CID}?as_of={AS_OF}").text
    card = html.split('id="support"')[1].split("</section>")[0]
    assert "Support messages" in card and "SYNTH-SUPPORT-Q" in card and "1 new" in card
    reply = form(card, f"/client/{CID}/support/reply")
    assert f'name="as_of" value="{AS_OF}"' in reply and 'name="message"' in reply
    read = form(card, f"/client/{CID}/support/read")
    assert f'name="as_of" value="{AS_OF}"' in read and "Mark read" in read
    chat = card.split('class="client-chat"')[1].split("</details>")[0]
    assert "<summary>Client&#39;s chat with Mr. J" in chat or "<summary>Client's chat with Mr. J" in chat
    assert "SYNTH-CLIENT-CHAT" in chat and "<form" not in chat  # read-only


def test_admin_mark_read_hidden_when_nothing_unread(env):
    _, admin, _ = env
    card = admin.get(f"/client/{CID}?as_of={AS_OF}").text.split('id="support"')[1].split("</section>")[0]
    assert "/support/read" not in card and "/support/reply" in card and "No messages yet." in card


def test_index_unread_badge(env):
    _, admin, db = env
    assert 'class="pill unread"' not in admin.get(f"/?as_of={AS_OF}").text
    SupportStore(EventStore(db)).add(CID, "client", "one")
    SupportStore(EventStore(db)).add(CID, "client", "two")
    assert '<span class="pill unread">2 new messages</span>' in admin.get(f"/?as_of={AS_OF}").text


def test_earlier_day_date_left_blank_so_server_today_applies(env):
    # A page left open past midnight must not save to the day it was rendered.
    client, _, _ = env
    html = client.get(f"/c/{CID}").text
    for path in ("weigh-in", "daily-target"):
        f = form(html, f"/c/{CID}/{path}")
        when = f.split('name="when"')[1].split(">")[0]
        assert f'max="{TODAY.isoformat()}"' in when and "value=" not in when


def test_coach_logged_checkin_notes_not_prefilled_for_client(env):
    from schemas.events import Source, make_event
    client, _, db = env
    EventStore(db).append([make_event(CID, "weekly_checkin", TODAY,
                                      {"hunger": 3, "energy": 3, "notes": "SYNTH-COACH-NOTE"}, Source.coach)])
    html = client.get(f"/c/{CID}").text
    assert "Saved for today" in html and "SYNTH-COACH-NOTE" not in html
    client.post(f"/c/{CID}/checkin", data={"hunger": "low", "energy": "mid", "notes": "SYNTH-MY-NOTE"})
    assert 'value="SYNTH-MY-NOTE"' in client.get(f"/c/{CID}").text


def test_chat_says_messages_go_to_claude(env):
    client, _, _ = env
    assert "Anthropic" in section(client.get(f"/c/{CID}").text, "chat")
