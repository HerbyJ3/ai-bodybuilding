"""Client web app — local prototype (BUILD_SPEC §15). Runs on 127.0.0.1 with no admin routes,
so it can later be hosted on its own. Clients pick their id at `/` (stand-in for a login) and
use `/c/{client_id}`. Dates come from the server's "today"; every event written is
Source.client. To the client, Mr. J is their coach; the human coach behind the scenes appears
only as "support". Pending/rejected/superseded proposals, queue internals, history review,
data-quality flags and the coach's chat are never shown here."""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from approvals.queue import ApprovalQueue
from config.loader import Config, load_config
from dashboard.app import (HERE as DASHBOARD_DIR, MACRO_KEYS, OFF_BY_CHOICES, _default_llm,
                           adherence_payload, asset_url, check_message, checkin_payload,
                           error_text, level_name, training_day_type, weigh_in_payload)
from dashboard.charts import weight_series, weight_svg
from dashboard.store import ChatStore, ClientRef, SupportStore, find_clients
from engine import data_quality
from engine.nutrition.macros import kcal_of
from engine.state_builder import build_state
from schemas.events import Source, make_event
from store.event_store import EventStore

HERE = Path(__file__).resolve().parent

# Cardio types for the Daily log (value stored as `modality`). Incline/speed only for incline walking.
CARDIO_TYPES: list[tuple[str, str]] = [
    ("treadmill_incline_walk", "Treadmill incline walk"),
    ("walk", "Walking (outdoor)"),
    ("run", "Running / jogging"),
    ("bike", "Stationary bike / cycling"),
    ("elliptical", "Elliptical"),
    ("stair_climber", "Stair climber"),
    ("rowing", "Rowing machine"),
    ("swim", "Swimming"),
]
INCLINE_CHOICES = list(range(1, 16))                       # treadmill incline, %
SPEED_CHOICES = [round(1.5 + 0.1 * i, 1) for i in range(31)]  # treadmill speed, mph (1.5–4.5)
EFFORT = {"easy": "low", "moderate": "mod", "hard": "high"}

# Looks a client can pick in Settings (value, label, one-line note); CSS lives in static/styles.css.
CLIENT_STYLES: list[tuple[str, str, str]] = [
    ("classic", "Classic", "Calm slate and sand (default)"),
    ("studio-calm", "Studio Calm", "Warm paper and navy. Quiet, light and easy on the eyes."),
    ("night-session", "Night Session", "Deep navy with sand highlights. Calm and focused, great at night."),
    ("retro-84", "Retro '84", "Neon sunset and grid lines. A fun throwback to the 80s."),
]


def create_client_app(data_dir: Path, cfg: Config | None = None,
                      llm_factory: Callable[[], Any] = _default_llm,
                      today: Callable[[], date] = date.today) -> FastAPI:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg or load_config()
    app = FastAPI(title="Mr. J", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
    app.mount("/app-static", StaticFiles(directory=HERE / "static"), name="app-static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals["asset"] = asset_url
    tpl.env.globals["level_name"] = level_name
    tpl.env.globals["off_by_choices"] = OFF_BY_CHOICES
    tpl.env.globals["client_styles"] = CLIENT_STYLES
    tpl.env.globals["cardio_types"] = CARDIO_TYPES
    tpl.env.globals["incline_choices"] = INCLINE_CHOICES
    tpl.env.globals["speed_choices"] = SPEED_CHOICES
    tpl.env.globals["weekday"] = lambda iso: date.fromisoformat(iso[:10]).strftime("%A")

    def client(cid: str) -> tuple[ClientRef, EventStore]:
        ref = find_clients(data_dir).get(cid)
        if ref is None:
            raise HTTPException(404, "unknown client")
        return ref, EventStore(ref.db_path)

    def back(cid: str, anchor: str, day: date | None = None, **params: str) -> RedirectResponse:
        if day is not None and day != today():
            params = {"day": day.isoformat(), **params}  # stay on the diary day the client was viewing
        q = urlencode({k: v for k, v in params.items() if v})
        return RedirectResponse(f"/c/{cid}" + (f"?{q}" if q else "") + f"#{anchor}", status_code=303)

    def when_or_today(value: str) -> date:
        """Optional date from a form: empty = today; never in the future."""
        d = today()
        if not value.strip():
            return d
        try:
            w = date.fromisoformat(value.strip())
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD")
        if w > d:
            raise ValueError("that date is in the future")
        return w

    def _number(value: str, what: str, most: float | None = None) -> float:
        try:
            x = float(value)
        except ValueError:
            raise ValueError(f"{what} must be a number")
        if not math.isfinite(x) or x < 0:
            raise ValueError(f"{what} must be a positive number")
        if most is not None and x > most:
            raise ValueError(f"{what} must be at most {most:g}")
        return x

    def diary_day(value: str) -> date:
        """Diary date from the address bar: blank, malformed or future = today."""
        try:
            d = date.fromisoformat(value.strip()) if value.strip() else today()
        except ValueError:
            return today()
        return min(d, today())

    def day_entries(events, d: date) -> dict[str, Any]:
        """What was logged on one diary day (latest wins per type, like the engine)."""
        checkin = adherence = None
        weigh_ins, cardio = [], []
        for e in events:
            if e.type == "weekly_checkin" and e.day == d:
                checkin = dict(e.payload.model_dump(), source=e.source.value)
                if e.source != Source.client:  # notes the coach typed stay private
                    checkin["notes"] = None
            elif e.type == "macro_adherence_logged" and e.payload.date == d:
                adherence = e.payload.model_dump()
            elif e.type == "weigh_in" and e.day == d:
                weigh_ins.append({"weight": e.payload.weight, "unit": e.payload.unit})
            elif e.type == "cardio_logged" and e.payload.date == d:
                cardio.append(e.payload.model_dump())
        return {"checkin": checkin, "daily_target": adherence, "weigh_ins": weigh_ins, "cardio": cardio}

    @app.get("/", response_class=HTMLResponse)
    def pick(request: Request):
        return tpl.TemplateResponse(request, "pick.html", {"clients": list(find_clients(data_dir))})

    @app.get("/c/{cid}", response_class=HTMLResponse)
    def home(request: Request, cid: str, day: str = "", error: str = "", notice: str = "", chat: str = ""):
        _, store = client(cid)
        d = today()
        shown = diary_day(day)
        events = store.read(cid)
        st = build_state(events, d, cfg)
        # Every weigh-in the client logged is drawn; readings data quality flags stay out of the
        # 7-day average so one bad reading can't skew the trend. Flags themselves are never shown.
        flagged = data_quality.assess(events, d, cfg).excluded_event_ids
        series = weight_series(events, d, set(), cfg.nutrition("tracking.weigh_ins_per_week")[0],
                               avg_excluded=flagged)
        targets = [{"day_type": k, "protein_g": round(m.protein_g), "carb_g": round(m.carb_g),
                    "fat_g": round(m.fat_g), "kcal": round(kcal_of(m, cfg))}
                   for k, m in (st.current_macros or {}).items()]
        changes = []
        for e in ApprovalQueue(store, cfg).approved(cid):  # approved/modified only, newest first
            if e.day > d:
                continue
            p = e.payload.proposal or {}
            changes.append({"date": e.day.isoformat(), "action": p.get("action"),
                            "target": p.get("target"), "final_value": e.payload.final_value})
        ctx = {
            "cid": cid, "today": d.isoformat(), "state": st,
            "chart": weight_svg(series, []), "series": series,
            "targets": targets, "changes": changes,
            "day": shown.isoformat(),
            "prev_day": (shown - timedelta(days=1)).isoformat(),
            "next_day": (shown + timedelta(days=1)).isoformat() if shown < d else None,
            "entries": day_entries(events, shown),
            "day_macros": build_state(events, shown, cfg).current_macros if shown != d else st.current_macros,
            "training_type": training_day_type(st.current_macros),
            "chat": ChatStore(store, "client").history(cid),
            "cardio_types": dict(CARDIO_TYPES),
            "support": SupportStore(store).thread(cid),
            "error": error, "notice": notice, "chat_open": chat == "open",
        }
        return tpl.TemplateResponse(request, "home.html", ctx)

    @app.post("/c/{cid}/weigh-in")
    def weigh_in(cid: str, weight: str = Form(""), unit: str = Form("lb"),
                 conditions: str = Form(""), day: str = Form(""), when: str = Form("")):
        _, store = client(cid)
        d = None
        try:
            d = when_or_today(day or when)  # the Daily log day being viewed; blank = today
            payload = weigh_in_payload(weight, unit, conditions)
            store.append([make_event(cid, "weigh_in", d, payload, Source.client)])
        except ValueError as exc:
            return back(cid, "diary", d, error=f"Weigh-in not saved: {error_text(exc)}")
        return back(cid, "diary", d, notice=f"Weigh-in saved: {payload['weight']:g} {unit}")

    @app.post("/c/{cid}/cardio")
    def cardio(cid: str, modality: str = Form(""), minutes: str = Form(""), kcal: str = Form(""),
               effort: str = Form("moderate"), incline: str = Form(""), speed: str = Form(""),
               day: str = Form("")):
        _, store = client(cid)
        d = None
        try:
            d = when_or_today(day)
            if modality not in dict(CARDIO_TYPES):
                raise ValueError("pick a cardio type")
            if effort not in EFFORT:
                raise ValueError("effort must be easy, moderate or hard")
            payload: dict[str, Any] = {"date": d, "modality": modality, "minutes": _number(minutes, "time", 600),
                                       "intensity": EFFORT[effort]}
            if kcal.strip():
                payload["est_kcal"] = _number(kcal, "calories burned", 3000)
            if modality == "treadmill_incline_walk":
                if incline.strip():
                    payload["incline_pct"] = _number(incline, "incline")
                if speed.strip():
                    payload["speed_mph"] = _number(speed, "speed")
            store.append([make_event(cid, "cardio_logged", d, payload, Source.client)])
        except ValueError as exc:
            return back(cid, "diary", d, error=f"Cardio not saved: {error_text(exc)}")
        return back(cid, "diary", d, notice="Cardio saved")

    @app.post("/c/{cid}/daily-target")
    async def daily_target(request: Request, cid: str):
        _, store = client(cid)
        form = await request.form()
        d = None
        try:
            d = when_or_today(str(form.get("day", "") or ""))
            payload = adherence_payload(
                d, form.get("hit") == "yes",
                {name: str(form.get(f"off_{name}", "") or "") for name in MACRO_KEYS},
                bool(form.get("non_training")), build_state(store.read(cid), d, cfg).current_macros)
            store.append([make_event(cid, "macro_adherence_logged", d, payload, Source.client)])
        except ValueError as exc:
            return back(cid, "diary", d, error=f"Not saved: {error_text(exc)}")
        return back(cid, "diary", d, notice="Daily target saved")

    @app.post("/c/{cid}/checkin")
    def checkin(cid: str, hunger: str = Form(""), energy: str = Form(""), training_feel: str = Form(""),
                sleep_hours: str = Form(""), notes: str = Form(""), day: str = Form("")):
        _, store = client(cid)
        d = None
        try:
            d = when_or_today(day)  # the diary day being viewed; blank = today
            payload = checkin_payload(hunger, energy, training_feel, sleep_hours, notes)
            # saving again for the same day replaces it (latest wins)
            store.append([make_event(cid, "weekly_checkin", d, payload, Source.client)])
        except ValueError as exc:
            return back(cid, "diary", d, error=f"Check-in not saved: {error_text(exc)}")
        return back(cid, "diary", d, notice="Check-in saved")

    @app.post("/c/{cid}/chat")
    def chat(cid: str, message: str = Form("")):
        from llm.coach import open_session
        _, store = client(cid)
        try:
            text = check_message(message)
        except ValueError as exc:
            return back(cid, "chat", chat="open", error=str(exc))
        chats = ChatStore(store, "client")
        history = [{"role": m["role"], "content": m["content"]} for m in chats.history(cid)]
        try:
            session = open_session(store, cfg, cid, today(), llm_factory(), audience="client_app",
                                   history=history)
            reply = session.ask(text)
        except Exception as exc:  # missing API key, network, no consent: show it, store nothing
            return back(cid, "chat", chat="open",
                        error=f"Mr. J couldn't answer right now ({exc.__class__.__name__}). "
                              "Please try again later.")
        chats.add(cid, "user", text)
        chats.add(cid, "assistant", reply.text)
        return back(cid, "chat", chat="open")

    @app.post("/c/{cid}/support")
    def support(cid: str, message: str = Form("")):
        _, store = client(cid)
        try:
            text = check_message(message)
        except ValueError as exc:
            return back(cid, "support", error=str(exc))
        SupportStore(store).add(cid, "client", text)
        return back(cid, "support", notice="Message sent to support")

    return app
