"""Coach CLI (Typer). `coach import-csv`, `coach onboard`, `coach state`, `coach proposals`."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer

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


if __name__ == "__main__":
    app()
