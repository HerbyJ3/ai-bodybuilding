"""Local coach dashboard (runs on 127.0.0.1; nothing leaves the machine except
Mr. J's calls to Claude). Start with `coach dashboard`."""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode
from datetime import date
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from approvals.queue import ApprovalQueue, QueueError
from config.loader import Config, load_config
from dashboard.charts import weight_series, weight_svg
from dashboard.store import ChatStore, ClientRef, find_clients
from engine import data_quality, history_review
from engine.state_builder import build_state
from ingest.onboarding import OnboardingError, load_onboarding_config, onboard
from store.event_store import EventStore

HERE = Path(__file__).resolve().parent
UPLOAD_NAME = re.compile(r"^[A-Za-z0-9._-]+\.(csv|json)$")
SAFE_DIR = re.compile(r"[^a-z0-9_-]+")


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
                          "limitations": len(st.limitations)})
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
        series = weight_series(events, d, excluded, cfg.nutrition("tracking.weigh_ins_per_week")[0])
        markers = [{"date": c["date"].isoformat(), "label": f"{c['day_type']} {c['kcal_change']:+d} kcal"}
                   for c in review["macro_changes"]]
        ctx = {
            "cid": cid, "db": ref.db_path, "as_of": d.isoformat(), "state": st, "review": review,
            "pending": [i for i in items if i.status == "pending"],
            "info": [i for i in items if i.status == "info"],
            "decided": [i for i in items if i.status in ("approved", "rejected", "modified")][-5:],
            "chart": weight_svg(series, markers), "series": series,
            "chat": ChatStore(store).history(cid), "error": error, "notice": notice,
            "intake": recent_intake(events, d, st),
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

    def _ask(cid: str, as_of: str, text: str, kind: str) -> str | None:
        from llm.coach import open_session
        _, store = client(cid)
        chats = ChatStore(store)
        history = [{"role": m["role"], "content": m["content"]} for m in chats.history(cid)]
        try:
            session = open_session(store, cfg, cid, day(as_of), llm_factory(), audience="coach",
                                   history=history)
            reply = session.ask(text)
        except Exception as exc:  # missing API key, network, etc.: show it, store nothing
            return f"Mr. J couldn't answer: {exc.__class__.__name__}: {exc}"
        chats.add(cid, "user", text, "chat")
        chats.add(cid, "assistant", reply.text, kind)
        return None

    @app.post("/client/{cid}/chat")
    def chat(cid: str, as_of: str = Form(""), message: str = Form(...)):
        if not message.strip():
            return back(cid, as_of, "chat")
        err = _ask(cid, as_of, message.strip(), "chat")
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
        ChatStore(store).clear(cid)
        return back(cid, as_of, "chat")

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
