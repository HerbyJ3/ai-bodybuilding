"""Coach CLI (Typer). `coach import-csv`, `coach onboard`, `coach state`, `coach proposals`."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer

from approvals.queue import ApprovalQueue, QueueError
from config.loader import load_config
from engine import history_review
from engine import proposals as proposal_engine
from engine.state_builder import build_state
from ingest.csv_import import import_csvs, load_mapping
from ingest.onboarding import OnboardingError, load_onboarding_config, onboard
from store.event_store import EventStore

app = typer.Typer(no_args_is_help=True, help="Hypertrophy & physique coach agent")
DEFAULT_DB = Path("data/coach.db")


@app.command("import-csv")
def import_csv(client_id: str, csv_dir: Path, mapping: Path, db: Path = DEFAULT_DB) -> None:
    """Import historical CSV logs (source=import). Requires recorded consent."""
    store = EventStore(db)
    if not store.has_consent(client_id):
        typer.echo(f"no consent recorded for {client_id}; refusing to import", err=True)
        raise typer.Exit(1)
    res = import_csvs(client_id, csv_dir, load_mapping(mapping))
    inserted, dupes = store.append(res.events)
    typer.echo(json.dumps({"inserted": inserted, "duplicates": dupes, "streams": res.summary()},
                          indent=2, default=str))


@app.command("import-mfp")
def import_mfp(client_id: str, files: list[Path], db: Path = DEFAULT_DB,
               unit: str = typer.Option("lb", help="weight unit when the export doesn't say (lb|kg)")) -> None:
    """Import MyFitnessPal reports: Premium export ZIP/CSVs or a saved Printable Diary (HTML)."""
    from ingest.myfitnesspal import import_paths
    store = EventStore(db)
    if not store.has_consent(client_id):
        typer.echo(f"no consent recorded for {client_id}; onboard the client first", err=True)
        raise typer.Exit(1)
    res = import_paths(client_id, files, unit)
    inserted, dupes = store.append(res.events)
    typer.echo(json.dumps({"inserted": inserted, "already_imported": dupes, **res.summary()},
                          indent=2, default=str))


@app.command("onboard")
def onboard_cmd(config: Path, db: Path = DEFAULT_DB, report: Path | None = None) -> None:
    """Mid-program onboarding: import history, backdate phase/meso, report gaps."""
    oc = load_onboarding_config(config)
    try:
        rep = onboard(EventStore(db), oc, load_config(), base_dir=config.parent)
    except OnboardingError as exc:
        typer.echo(f"onboarding failed: {exc}", err=True)
        raise typer.Exit(1)
    out = rep.to_json()
    if report:
        report.write_text(out, encoding="utf-8")
    typer.echo(out)


@app.command("state")
def state_cmd(client_id: str, as_of: str, db: Path = DEFAULT_DB) -> None:
    st = build_state(EventStore(db).read(client_id), date.fromisoformat(as_of), load_config())
    typer.echo(st.model_dump_json(indent=2))


@app.command("proposals")
def proposals_cmd(client_id: str, as_of: str, db: Path = DEFAULT_DB) -> None:
    cfg = load_config()
    st = build_state(EventStore(db).read(client_id), date.fromisoformat(as_of), cfg)
    for p in proposal_engine.generate(st, cfg):
        typer.echo(p.model_dump_json())


@app.command("review")
def review_cmd(client_id: str, as_of: str, db: Path = DEFAULT_DB, full: bool = False) -> None:
    """History review: what progressed, what stalled, what to improve (read-only)."""
    cfg = load_config()
    events = EventStore(db).read(client_id)
    st = build_state(events, date.fromisoformat(as_of), cfg)
    rev = history_review.review(events, st, cfg)
    if full:
        typer.echo(json.dumps(rev, indent=2, default=str))
    else:
        for f in rev["findings"]:
            typer.echo(f"[{f['area']}] {f['finding']}")


queue_app = typer.Typer(no_args_is_help=True, help="Approval queue: coach approves, rejects or modifies proposals")
app.add_typer(queue_app, name="queue")


def _queue(db: Path) -> ApprovalQueue:
    return ApprovalQueue(EventStore(db), load_config())


def _line(item) -> str:
    p = item.proposal
    flags = f"  flags: {', '.join(p.data_quality_flags)}" if p.data_quality_flags else ""
    return (f"{p.proposal_id[:8]}  {item.status:<10} {p.confidence:<6} {p.action:<22} "
            f"{p.target:<22} {p.rationale_short}{flags}")


@queue_app.command("refresh")
def queue_refresh(client_id: str, as_of: str, db: Path = DEFAULT_DB) -> None:
    """Run the rules engine and add new proposals to the queue."""
    q = _queue(db)
    try:
        q.refresh(client_id, date.fromisoformat(as_of))
    except QueueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for item in q.pending(client_id):
        typer.echo(_line(item))


@queue_app.command("list")
def queue_list(client_id: str, db: Path = DEFAULT_DB,
               all_: bool = typer.Option(False, "--all", help="include decided, superseded and info")) -> None:
    """List pending proposals (with --all: everything)."""
    q = _queue(db)
    items = q.items(client_id) if all_ else q.pending(client_id)
    if not items:
        typer.echo("nothing pending")
    for item in items:
        typer.echo(_line(item))


def _resolve(q: ApprovalQueue, prefix: str) -> str:
    rows = q.store._db.execute("SELECT proposal_id FROM proposals WHERE proposal_id LIKE ?",
                               (prefix + "%",)).fetchall()
    if len(rows) != 1:
        typer.echo(f"{'no' if not rows else 'ambiguous'} proposal matching '{prefix}'", err=True)
        raise typer.Exit(1)
    return rows[0][0]


@queue_app.command("show")
def queue_show(proposal_id: str, db: Path = DEFAULT_DB) -> None:
    """Show a proposal in full: inputs, config keys, confidence, rationale."""
    q = _queue(db)
    item = q.get(_resolve(q, proposal_id))
    typer.echo(json.dumps({"status": item.status, **json.loads(item.proposal.model_dump_json())}, indent=2))


def _decide(proposal_id: str, decision: str, note: str, value: str | None, db: Path) -> None:
    q = _queue(db)
    try:
        q.decide(_resolve(q, proposal_id), decision, note, json.loads(value) if value else None)
    except (QueueError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"{decision}: {proposal_id}")


@queue_app.command("approve")
def queue_approve(proposal_id: str, note: str = "", db: Path = DEFAULT_DB) -> None:
    """Approve as proposed (an id prefix is enough)."""
    _decide(proposal_id, "approved", note, None, db)


@queue_app.command("reject")
def queue_reject(proposal_id: str, note: str = typer.Option(..., help="why (required)"),
                 db: Path = DEFAULT_DB) -> None:
    """Reject with a note."""
    _decide(proposal_id, "rejected", note, None, db)


@queue_app.command("modify")
def queue_modify(proposal_id: str, value: str = typer.Option(..., help="JSON value that takes effect"),
                 note: str = typer.Option(..., help="why (required)"), db: Path = DEFAULT_DB) -> None:
    """Approve with the coach's own value (JSON) and a note."""
    _decide(proposal_id, "modified", note, value, db)


def _session(client_id: str, as_of: str, db: Path, offline: bool = False, audience: str = "client"):
    """`audience="client"`: Mr. J writes for the client (check-in); "coach": the owner talks to him."""
    from llm.coach import open_session
    llm = None
    if not offline:
        from llm.client import ClaudeClient
        from llm.settings import llm_settings
        llm = ClaudeClient(llm_settings())
    try:
        day = date.fromisoformat(as_of) if as_of else date.today()
        return open_session(EventStore(db), load_config(), client_id, day, llm, audience=audience)
    except PermissionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command("prompt")
def prompt_cmd(client_id: str, as_of: str = typer.Argument(None, help="YYYY-MM-DD, default today"),
           db: Path = DEFAULT_DB) -> None:
    """Print the assembled system prompt (no API call): what Mr. J would see."""
    typer.echo(_session(client_id, as_of, db, offline=True).system)


@app.command("checkin")
def checkin_cmd(client_id: str, as_of: str = typer.Argument(None, help="YYYY-MM-DD, default today"),
           db: Path = DEFAULT_DB) -> None:
    """Mr. J writes the weekly check-in message from approved changes and findings."""
    from llm.coach import CHECKIN_REQUEST
    typer.echo(_session(client_id, as_of, db).ask(CHECKIN_REQUEST).text)


@app.command("chat")
def chat_cmd(client_id: str, as_of: str = typer.Argument(None, help="YYYY-MM-DD, default today"),
           db: Path = DEFAULT_DB) -> None:
    """Chat with Mr. J about this client (empty line or Ctrl-D to quit)."""
    session = _session(client_id, as_of, db, audience="coach")  # the coach is the one chatting
    while True:
        try:
            text = input("you> ").strip()
        except EOFError:
            break
        if not text:
            break
        typer.echo(f"\nMr. J> {session.ask(text).text}\n")


@app.command("dashboard")
def dashboard_cmd(data_dir: Path = typer.Option(Path("data"), help="folder holding client databases"),
                  port: int = 8765, open_browser: bool = typer.Option(True, "--open/--no-open")) -> None:
    """Open the local coach dashboard in your browser (runs on this computer only)."""
    import threading
    import webbrowser

    import uvicorn

    from dashboard.app import create_app
    url = f"http://127.0.0.1:{port}"
    typer.echo(f"Mr. J dashboard: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(data_dir), host="127.0.0.1", port=port, log_level="warning")


@app.command("client-app")
def client_app_cmd(data_dir: Path = typer.Option(Path("data"), help="folder holding client databases"),
                   port: int = 8766, open_browser: bool = typer.Option(True, "--open/--no-open")) -> None:
    """Open the local client app prototype (runs on this computer only; no logins yet)."""
    import threading
    import webbrowser

    import uvicorn

    from clientapp.app import create_client_app
    url = f"http://127.0.0.1:{port}"
    typer.echo(f"Mr. J client app: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_client_app(data_dir), host="127.0.0.1", port=port, log_level="warning")


@app.command("fetch-assets")
def fetch_assets_cmd() -> None:
    """Save the dashboard graphics (made with Higgsfield) locally for offline use."""
    from dashboard.app import fetch_assets
    for path in fetch_assets():
        typer.echo(f"saved {path}")


@app.command("eval")
def eval_cmd(llm: bool = typer.Option(False, "--llm", help="also run Mr. J scenarios (Claude API, costs money)"),
             only: str = typer.Option(None, help="run one scenario id"),
             json_out: Path = typer.Option(None, "--json", help="write a JSON report")) -> None:
    """Run the M9 eval scenarios (evals/scenarios.yaml)."""
    from evals.run import main
    args = (["--llm"] if llm else []) + (["--only", only] if only else []) + \
        (["--json", str(json_out)] if json_out else [])
    raise typer.Exit(main(args))


if __name__ == "__main__":
    app()
