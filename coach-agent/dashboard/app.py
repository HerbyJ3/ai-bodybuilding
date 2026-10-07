"""Local coach dashboard (runs on 127.0.0.1; nothing leaves the machine except
Mr. J's calls to Claude). Start with `coach dashboard`."""
from __future__ import annotations

import json
import math
import re
from urllib.parse import urlencode
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from approvals.queue import ApprovalQueue, QueueError
from config.loader import Config, load_config
from dashboard.charts import weight_series, weight_svg
from dashboard.store import ChatStore, ClientRef, SupportStore, find_clients
from engine import data_quality, history_review
from engine.state_builder import build_state
from ingest.onboarding import OnboardingError, load_onboarding_config, onboard
from store.event_store import EventStore

HERE = Path(__file__).resolve().parent
LEVELS = {"low": 1, "mid": 3, "high": 5}  # dashboard hunger/energy -> engine 1-5 scale
LEVEL_NAMES = {1: "low", 3: "mid", 5: "high"}
MACRO_KEYS = {"protein": "protein_g", "carbs": "carb_g", "fat": "fat_g"}
OFF_BY_CHOICES = [-100, -75, -50, -40, -30, -20, -10, 10, 20, 30, 40, 50, 75, 100]
UPLOAD_NAME = re.compile(r"^[A-Za-z0-9._-]+\.(csv|json)$")
SAFE_DIR = re.compile(r"[^a-z0-9_-]+")
MAX_MESSAGE = 4000  # characters per chat/support message (same limit as the UI)


def check_message(text: str) -> str:
    """Trimmed message text; raises ValueError when empty or too long."""
    text = (text or "").strip()
    if not text:
        raise ValueError("please write a message first")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"messages are limited to {MAX_MESSAGE} characters")
    return text


def training_day_type(macros: dict | None) -> str | None:
    """The client's training-day targets: the first day type that isn't the rest (non_training) day."""
    return next((k for k in (macros or {}) if k != "non_training"), None)


def checkin_payload(hunger: str, energy: str, training_feel: str = "", sleep_hours: str = "",
                    notes: str = "") -> dict[str, Any]:
    """Check-in form -> weekly_checkin payload (raises ValueError on bad input)."""
    if hunger not in LEVELS or energy not in LEVELS:
        raise ValueError("hunger and energy must be low, mid or high")
    payload: dict[str, Any] = {"hunger": LEVELS[hunger], "energy": LEVELS[energy], "notes": notes.strip()}
    if training_feel:
        payload["training_feel"] = training_feel
    if sleep_hours:
        h = float(sleep_hours)
        if not math.isfinite(h):
            raise ValueError("sleep must be a number of hours")
        payload["sleep_hours"] = h
    return payload


def weigh_in_payload(weight: str, unit: str, conditions: str = "") -> dict[str, Any]:
    """Weigh-in form -> weigh_in payload (raises ValueError on bad input)."""
    w = float(weight)
    if not math.isfinite(w):
        raise ValueError("weight must be a number")
    if unit not in ("lb", "kg"):
        raise ValueError("unit must be lb or kg")
    return {"weight": w, "unit": unit, "conditions": conditions.strip() or "unspecified"}


def adherence_payload(when: date, hit: bool, off: dict[str, str], non_training: bool,
                      macros: dict | None) -> dict[str, Any]:
    """Daily Target form -> macro_adherence_logged payload. `off` maps protein/carbs/fat to the
    chosen grams (ignored when hit); `macros` are the client's current targets on that date."""
    off_by = {}
    if not hit:
        for name, key in MACRO_KEYS.items():
            v = str(off.get(name, "") or "")
            if v:
                g = float(v)
                if not math.isfinite(g):
                    raise ValueError("off-by amounts must be numbers")
                off_by[key] = g
    day_type = "non_training" if non_training else training_day_type(macros)
    return {"date": when, "hit": hit, "off_by": off_by, "day_type": day_type}


def error_text(exc: Exception) -> str:
    """The readable part of an error: a pydantic error's first message (field: reason), otherwise
    the last line, skipping pydantic's 'For further information visit <url>' footer."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            field = ".".join(str(p) for p in first.get("loc", ()))
            return f"{field}: {first['msg']}" if field else str(first["msg"])
        except Exception:  # not a pydantic error after all: fall back to the text
            pass
    lines = [ln.strip() for ln in str(exc).strip().splitlines()
             if ln.strip() and not ln.strip().startswith("For further information")]
    return lines[-1] if lines else exc.__class__.__name__


def level_name(v: Any) -> str:
    """1/3/5 come from the dashboard (low/mid/high); 2/4 from 1-5 imports are shown with the number."""
    if v in LEVEL_NAMES:
        return LEVEL_NAMES[v]
    if v in (2, 4):
        return f"{'low' if v == 2 else 'high'} ({v})"
    return "–" if v is None else str(v)


def load_assets() -> dict[str, dict[str, str]]:
    return {k: v for k, v in json.loads((HERE / "static" / "assets.json").read_text()).items()
            if not k.startswith("_")}


def asset_url(name: str) -> str:
    """Local copy if fetched, else the Higgsfield-hosted original."""
    a = load_assets()[name]
    return f"/static/{a['file']}" if (HERE / "static" / a["file"]).exists() else a["url"]


def fetch_assets() -> list[str]:
    import urllib.request
    done = []
    for name, a in load_assets().items():
        target = HERE / "static" / a["file"]
        with urllib.request.urlopen(a["url"], timeout=30) as r:
            target.write_bytes(r.read())
        done.append(str(target))
    return done


def recent_intake(events, as_of: date, state, days: int = 14) -> list[dict[str, Any]]:
    """Last `days` logged days (latest log per date wins), newest first, with the moderate/first
    day-type calorie target for comparison."""
    from engine.nutrition.macros import kcal_of
    latest = {}
    for e in events:
        if e.type == "intake_logged" and 0 <= (as_of - e.payload.date).days < days:
            latest[e.payload.date] = e.payload
    targets = {k: round(kcal_of(m, load_config())) for k, m in (state.current_macros or {}).items()}
    return [{"date": d, "calories": round(p.calories), "protein_g": round(p.protein_g),
             "carb_g": round(p.carb_g), "fat_g": round(p.fat_g), "targets": targets}
            for d, p in sorted(latest.items(), reverse=True)]


def _default_llm():
    from llm.client import ClaudeClient
    from llm.settings import llm_settings
    return ClaudeClient(llm_settings())


def create_app(data_dir: Path, cfg: Config | None = None,
               llm_factory: Callable[[], Any] = _default_llm) -> FastAPI:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg or load_config()
    app = FastAPI(title="Mr. J dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, default=str)
    tpl.env.globals["asset"] = asset_url
    tpl.env.globals["level_name"] = level_name
    tpl.env.globals["off_by_choices"] = OFF_BY_CHOICES

    def client(cid: str) -> tuple[ClientRef, EventStore]:
        ref = find_clients(data_dir).get(cid)
        if ref is None:
            raise HTTPException(404, f"unknown client {cid}")
        return ref, EventStore(ref.db_path)

    def day(as_of: str | None) -> date:
        try:
            return date.fromisoformat(as_of) if as_of else date.today()
        except ValueError:
            raise HTTPException(400, "as_of must be YYYY-MM-DD")

    def back(cid: str, as_of: str | None, anchor: str = "", **params: str) -> RedirectResponse:
        q = urlencode({k: v for k, v in {"as_of": as_of or "", **params}.items() if v})
        return RedirectResponse(f"/client/{cid}?{q}#{anchor}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, as_of: str | None = None):
        d = day(as_of)
        cards = []
        for cid, ref in find_clients(data_dir).items():
            store = EventStore(ref.db_path)
            events = store.read(cid)
            try:
                st = build_state(events, d, cfg)
            except ValueError:
                continue
            pending = len(ApprovalQueue(store, cfg).pending(cid))
            cards.append({"id": cid, "state": st, "pending": pending,
                          "limitations": len(st.limitations),
                          "support_unread": SupportStore(store).unread_for_coach(cid)})
        return tpl.TemplateResponse(request, "index.html", {"cards": cards, "as_of": d.isoformat()})

    @app.get("/client/{cid}", response_class=HTMLResponse)
    def client_page(request: Request, cid: str, as_of: str | None = None, error: str = "",
                    notice: str = ""):
        ref, store = client(cid)
        d = day(as_of)
        events = store.read(cid)
        st = build_state(events, d, cfg)
        review = history_review.review(events, st, cfg)
        queue = ApprovalQueue(store, cfg)
        items = queue.items(cid)
        excluded = data_quality.assess(events, d, cfg).excluded_event_ids
        support = SupportStore(store)
        series = weight_series(events, d, excluded, cfg.nutrition("tracking.weigh_ins_per_week")[0])
        markers = [{"date": c["date"].isoformat(), "label": f"{c['day_type']} {c['kcal_change']:+d} kcal"}
                   for c in review["macro_changes"]]
        ctx = {
            "cid": cid, "db": ref.db_path, "as_of": d.isoformat(), "state": st, "review": review,
            "pending": [i for i in items if i.status == "pending"],
            "info": [i for i in items if i.status == "info"],
            "decided": [i for i in items if i.status in ("approved", "rejected", "modified")][-5:],
            "chart": weight_svg(series, markers), "series": series,
            "chat": ChatStore(store, "coach").history(cid), "error": error, "notice": notice,
            # read-only: what the client and Mr. J said to each other (owner default D1)
            "client_chat": ChatStore(store, "client").history(cid),
            "support": support.thread(cid), "support_unread": support.unread_for_coach(cid),
            "intake": recent_intake(events, d, st),
            "checkin_today": next((c for c in st.checkins_recent if c["date"] == d), None),
            "training_type": training_day_type(st.current_macros),
        }
        return tpl.TemplateResponse(request, "client.html", ctx)

    @app.post("/client/{cid}/queue/refresh")
    def refresh(cid: str, as_of: str = Form("")):
        _, store = client(cid)
        try:
            ApprovalQueue(store, cfg).refresh(cid, day(as_of))
        except QueueError as exc:
            return back(cid, as_of, "queue", error=str(exc))
        return back(cid, as_of, "queue")

    @app.post("/client/{cid}/proposal/{pid}/{decision}")
    def decide(cid: str, pid: str, decision: str, as_of: str = Form(""), note: str = Form(""),
               value: str = Form("")):
        _, store = client(cid)
        mapping = {"approve": "approved", "reject": "rejected", "modify": "modified"}
        if decision not in mapping:
            raise HTTPException(404)
        try:
            parsed = json.loads(value) if decision == "modify" and value.strip() else None
            ApprovalQueue(store, cfg).decide(pid, mapping[decision], note, parsed)
        except (QueueError, json.JSONDecodeError) as exc:
            return back(cid, as_of, "queue", error=f"{decision} failed: {exc}")
        return back(cid, as_of, "queue")

    def _when(value: str, as_of: str) -> date:
        return day(value or as_of)

    @app.post("/client/{cid}/checkin-log")
    def checkin_log(cid: str, as_of: str = Form(""), hunger: str = Form(...),
                    energy: str = Form(...), training_feel: str = Form(""), sleep_hours: str = Form(""),
                    notes: str = Form("")):
        from schemas.events import Source, make_event
        _, store = client(cid)
        try:
            payload = checkin_payload(hunger, energy, training_feel, sleep_hours, notes)
        except ValueError as exc:
            return back(cid, as_of, "checkins", error=str(exc))
        try:
            # dated by the page's "As of" date; saving again for that date replaces the entry
            store.append([make_event(cid, "weekly_checkin", day(as_of), payload, Source.coach)])
        except ValueError as exc:
            return back(cid, as_of, "checkins", error=f"check-in not saved: {exc}")
        return back(cid, as_of, "checkins", notice="Check-in saved")

    @app.post("/client/{cid}/weigh-in")
    def weigh_in(cid: str, as_of: str = Form(""), when: str = Form(""), weight: str = Form(...),
                 unit: str = Form("lb"), conditions: str = Form("")):
        from schemas.events import Source, make_event
        _, store = client(cid)
        try:
            payload = weigh_in_payload(weight, unit, conditions)
            store.append([make_event(cid, "weigh_in", _when(when, as_of), payload, Source.coach)])
        except ValueError as exc:
            return back(cid, as_of, "weigh-in", error=f"weigh-in not saved: {error_text(exc)}")
        return back(cid, as_of, "weigh-in", notice=f"Weigh-in saved: {payload['weight']:g} {unit}")

    @app.post("/client/{cid}/macros-hit")
    async def macros_hit(request: Request, cid: str):
        from schemas.events import Source, make_event
        _, store = client(cid)
        form = await request.form()
        as_of = str(form.get("as_of", ""))
        when = _when(str(form.get("when", "")), as_of)
        try:
            payload = adherence_payload(
                when, form.get("hit") == "yes",
                {name: str(form.get(f"off_{name}", "") or "") for name in MACRO_KEYS},
                bool(form.get("non_training")), build_state(store.read(cid), when, cfg).current_macros)
            ev = make_event(cid, "macro_adherence_logged", when, payload, Source.coach)
            store.append([ev])
        except ValueError as exc:
            return back(cid, as_of, "macros", error=f"not saved: {error_text(exc)}")
        return back(cid, as_of, "macros", notice="Daily target logged")

    @app.post("/client/{cid}/import-mfp")
    async def import_mfp(cid: str, as_of: str = Form(""), unit: str = Form("lb"),
                         files: list[UploadFile] = File(...)):
        from ingest.myfitnesspal import parse_files
        _, store = client(cid)
        if unit not in ("lb", "kg"):
            unit = "lb"
        uploads = {}
        for f in files:
            name = Path(f.filename or "").name
            if not re.match(r"^[A-Za-z0-9 ._()-]+\.(csv|zip|html?|pdf)$", name, re.IGNORECASE):
                return back(cid, as_of, "food", error=f"'{name}' is not a MyFitnessPal report file")
            uploads[name] = await f.read()
        res = parse_files(cid, uploads, unit)
        inserted, dupes = store.append(res.events)
        parts = [f"{i['name']}: {i['kind']}, {i['imported']} rows" + (f" ({'; '.join(i['notes'])})"
                                                                      if i["notes"] else "")
                 for i in res.files]
        msg = f"MyFitnessPal import: {inserted} new, {dupes} already imported. " + " | ".join(parts)
        return back(cid, as_of, "food", notice=msg)

    def _ask(cid: str, as_of: str, text: str, kind: str,
             attachments: list[tuple[str, bytes]] | None = None) -> str | None:
        from llm.attachments import AttachmentError, to_block
        from llm.coach import open_session
        ref, store = client(cid)
        chats = ChatStore(store, "coach")
        history = [{"role": m["role"], "content": m["content"]} for m in chats.history(cid)]
        try:
            blocks = [to_block(n, b) for n, b in attachments or []]
        except AttachmentError as exc:
            return str(exc)
        try:
            session = open_session(store, cfg, cid, day(as_of), llm_factory(), audience="coach",
                                   history=history)
            reply = session.ask(text, blocks or None)
        except Exception as exc:  # missing API key, network, etc.: show it, store nothing
            return f"Mr. J couldn't answer: {exc.__class__.__name__}: {exc}"
        saved_note = ""
        if attachments:
            folder = ref.db_path.parent / "attachments"
            folder.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for n, b in attachments:
                (folder / f"{stamp}-{n}").write_bytes(b)
            saved_note = "\n\n[Attached: " + ", ".join(n for n, _ in attachments) + "]"
        chats.add(cid, "user", text + saved_note, "chat")
        chats.add(cid, "assistant", reply.text, kind)
        return None

    @app.post("/client/{cid}/chat")
    async def chat(cid: str, as_of: str = Form(""), message: str = Form(""),
                   files: list[UploadFile] = File(default=[])):
        uploads = []
        for f in files:
            name = Path(f.filename or "").name
            if not name:
                continue  # empty file input
            if not re.match(r"^[A-Za-z0-9 ._()-]+$", name):
                return back(cid, as_of, "chat", error=f"'{name}': please rename the file (letters, numbers, . _ - only)")
            uploads.append((name, await f.read()))
        if len(uploads) > 3:
            return back(cid, as_of, "chat", error="attach up to 3 files at a time")
        text = message.strip() or ("Here's a training file for this client. Please review it."
                                   if uploads else "")
        if not text:
            return back(cid, as_of, "chat")
        err = _ask(cid, as_of, text, "chat", uploads)
        return back(cid, as_of, "chat", error=err or "")

    @app.post("/client/{cid}/checkin")
    def checkin(cid: str, as_of: str = Form("")):
        from llm.coach import CHECKIN_REQUEST
        err = _ask(cid, as_of, "Draft this week's check-in message to the client. " + CHECKIN_REQUEST,
                   "draft")
        return back(cid, as_of, "chat", error=err or "")

    @app.post("/client/{cid}/chat/clear")
    def clear_chat(cid: str, as_of: str = Form("")):
        _, store = client(cid)
        ChatStore(store, "coach").clear(cid)
        return back(cid, as_of, "chat")

    @app.post("/client/{cid}/support/reply")
    def support_reply(cid: str, as_of: str = Form(""), message: str = Form("")):
        _, store = client(cid)
        try:
            text = check_message(message)
        except ValueError as exc:
            return back(cid, as_of, "support", error=f"reply not sent: {exc}")
        support = SupportStore(store)
        support.add(cid, "coach", text)
        support.mark_read(cid)  # replying means the coach has read the thread
        return back(cid, as_of, "support", notice="Reply sent")

    @app.post("/client/{cid}/support/read")
    def support_read(cid: str, as_of: str = Form("")):
        _, store = client(cid)
        SupportStore(store).mark_read(cid)
        return back(cid, as_of, "support")

    @app.get("/onboard", response_class=HTMLResponse)
    def onboard_form(request: Request):
        return tpl.TemplateResponse(request, "onboard.html", {"report": None, "error": ""})

    @app.post("/onboard", response_class=HTMLResponse)
    async def onboard_upload(request: Request, files: list[UploadFile] = File(...)):
        saved: dict[str, bytes] = {}
        for f in files:
            name = Path(f.filename or "").name
            if not UPLOAD_NAME.match(name):
                return tpl.TemplateResponse(request, "onboard.html", {
                    "report": None, "error": f"'{name}' is not allowed: only .csv and .json files"})
            saved[name] = await f.read()
        if "onboarding.json" not in saved:
            return tpl.TemplateResponse(request, "onboard.html", {
                "report": None, "error": "onboarding.json is required"})
        try:
            cid = json.loads(saved["onboarding.json"])["client_id"]
        except (json.JSONDecodeError, KeyError):
            return tpl.TemplateResponse(request, "onboard.html", {
                "report": None, "error": "onboarding.json has no client_id"})
        folder = data_dir / (SAFE_DIR.sub("-", str(cid).lower()).strip("-") or "client")
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in saved.items():
            (folder / name).write_bytes(content)
        try:
            oc = load_onboarding_config(folder / "onboarding.json")
            report = onboard(EventStore(folder / "coach.db"), oc, cfg, base_dir=folder).data
        except (OnboardingError, ValueError, FileNotFoundError) as exc:
            return tpl.TemplateResponse(request, "onboard.html", {"report": None, "error": str(exc)})
        return tpl.TemplateResponse(request, "onboard.html", {"report": report, "error": "",
                                                              "cid": oc.client_id})

    return app
