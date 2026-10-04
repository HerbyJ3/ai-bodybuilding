"""MyFitnessPal report import. Synthetic files shaped like MyFitnessPal's Premium export
and Printable Diary; no real data."""
import io
import json
import zipfile
from datetime import date, timedelta

import pytest

from engine.state_builder import build_state
from ingest.myfitnesspal import parse_date, parse_files
from store.event_store import EventStore
from tests.conftest import CID, ev

NUTRITION = (
    "Date,Meal,Calories,Fat (g),Saturated Fat,Polyunsaturated Fat,Monounsaturated Fat,Trans Fat,"
    "Cholesterol,Sodium (mg),Potassium,Carbohydrates (g),Fiber,Sugar,Protein (g),Vitamin A,Vitamin C,"
    "Calcium,Iron,Note\n"
    "2026-09-28,Breakfast,550.0,15.0,4,1,2,0,30,400,300,60.0,8,10,45.0,0,0,0,0,\n"
    "2026-09-28,Lunch,700.0,20.0,5,1,2,0,60,700,500,70.0,6,5,60.0,0,0,0,0,\n"
    "2026-09-28,Dinner,\"1,000.0\",30.0,8,2,3,0,90,900,800,90.0,10,8,90.0,0,0,0,0,late meal\n"
    "2026-09-29,Breakfast,600.0,18.0,4,1,2,0,30,400,300,55.0,8,10,50.0,0,0,0,0,\n"
)
PROGRESS = "Date,Weight,Body Fat %\n2026-09-28,205.3,31.3\n2026-09-29,207.6,\n2026-09-30,,31.0\n"
EXERCISE = (
    "Date,Exercise,Type,Exercise Calories,Exercise Minutes,Sets,Reps Per Set,Kilograms,Steps,Note\n"
    "2026-09-28,\"Walking, incline\",Cardio,220,35,,,,,\n"
    "2026-09-28,Smith machine squat,Strength,,,4,10,60,,\n"
    "2026-09-29,Steps,Cardio,150,,,,,8000,\n"
)
DIARY_HTML = """<html><body>
<h2 class="main-title-2">Monday, September 28, 2026</h2>
<table class="table0" id="food"><thead><tr><td class="first">Foods</td><td>Calories</td><td>Carbs</td>
<td>Fat</td><td>Protein</td><td>Cholest</td><td>Sodium</td><td>Sugars</td><td>Fiber</td></tr></thead>
<tbody><tr class="title"><td class="first" colspan="9">Breakfast</td></tr>
<tr><td class="first">Oats</td><td>300</td><td>54g</td><td>5g</td><td>10g</td><td>0mg</td><td>0mg</td><td>1g</td><td>8g</td></tr></tbody>
<tfoot><tr><td class="first">TOTAL:</td><td>2,250</td><td>215g</td><td>65g</td><td>220g</td><td>0mg</td><td>0mg</td><td>0g</td><td>25g</td></tr></tfoot></table>
<table id="excercise"><thead><tr><td>Exercises</td><td>Calories</td><td>Minutes</td></tr></thead>
<tfoot><tr><td>TOTALS:</td><td>220</td><td>35</td></tr></tfoot></table>
<h2 class="main-title-2">Tuesday, September 29, 2026</h2>
<table class="table0"><thead><tr><td>Foods</td><td>Calories</td><td>Carbs</td><td>Fat</td><td>Protein</td></tr></thead>
<tfoot><tr><td>TOTAL:</td><td>2,030</td><td>130g</td><td>70g</td><td>220g</td></tr></tfoot></table>
</body></html>"""


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return buf.getvalue()


def _by_type(res, t):
    return [e for e in res.events if e.type == t]


def test_nutrition_meals_summed_per_day():
    res = parse_files(CID, {"Nutrition-Summary-2026-09-28-to-2026-09-29.csv": NUTRITION.encode()})
    days = {e.payload.date: e.payload for e in _by_type(res, "intake_logged")}
    d = days[date(2026, 9, 28)]
    assert (d.calories, d.protein_g, d.carb_g, d.fat_g) == (2250.0, 195.0, 220.0, 65.0)
    assert days[date(2026, 9, 29)].calories == 600.0
    assert res.files[0]["kind"] == "nutrition" and all(e.source.value == "import" for e in res.events)


def test_progress_weights_only_and_other_measurements_noted():
    res = parse_files(CID, {"Measurement-Summary.csv": PROGRESS.encode()})
    w = _by_type(res, "weigh_in")
    assert [(e.day, e.payload.weight, e.payload.unit) for e in w] == [
        (date(2026, 9, 28), 205.3, "lb"), (date(2026, 9, 29), 207.6, "lb")]
    assert any("Body Fat" in n for n in res.files[0]["notes"])


def test_kg_header_and_us_dates():
    res = parse_files(CID, {"p.csv": b"Date,Weight (kg)\n09/28/2026,93.1\n"})
    [w] = _by_type(res, "weigh_in")
    assert (w.day, w.payload.unit) == (date(2026, 9, 28), "kg")


def test_exercise_cardio_kept_strength_and_steps_skipped():
    res = parse_files(CID, {"Exercise-Summary.csv": EXERCISE.encode()})
    [c] = _by_type(res, "cardio_logged")
    assert (c.payload.modality, c.payload.minutes, c.payload.est_kcal) == ("Walking, incline", 35, 220)
    assert any("skipped" in n for n in res.files[0]["notes"])


def test_premium_zip_with_all_three_reports():
    z = _zip({"File-Export-2026/Nutrition-Summary.csv": NUTRITION, "File-Export-2026/Measurement-Summary.csv": PROGRESS,
              "File-Export-2026/Exercise-Summary.csv": EXERCISE, "__MACOSX/._junk": "x"})
    res = parse_files(CID, {"File-Export-2026.zip": z})
    assert {f["kind"] for f in res.files if f["imported"]} == {"nutrition", "progress", "exercise"}
    assert res.summary()["events"] == {"cardio_logged": 1, "intake_logged": 2, "weigh_in": 2}


def test_printable_diary_html_food_totals():
    res = parse_files(CID, {"diary.html": DIARY_HTML.encode()})
    days = {e.payload.date: e.payload for e in _by_type(res, "intake_logged")}
    assert (days[date(2026, 9, 28)].calories, days[date(2026, 9, 28)].protein_g,
            days[date(2026, 9, 28)].carb_g, days[date(2026, 9, 28)].fat_g) == (2250, 220, 215, 65)
    assert days[date(2026, 9, 29)].carb_g == 130  # exercise TOTALS row not mistaken for food


@pytest.mark.parametrize("name,data,expect", [
    ("report.pdf", b"%PDF-1.4", "save page as"),
    ("notes.txt", b"hello", "unsupported"),
    ("random.csv", b"Name,Value\na,1\n", "no date"),
    ("other.csv", b"Date,Mood\n2026-09-28,good\n", "not recognised"),
    ("broken.zip", b"not a zip", "not a valid zip"),
])
def test_unsupported_inputs_reported_not_imported(name, data, expect):
    res = parse_files(CID, {name: data})
    assert res.events == [] and expect in " ".join(res.files[0]["notes"]).lower()


def test_parse_date_formats():
    for s in ("2026-09-28", "09/28/2026", "September 28, 2026", "Monday, September 28, 2026"):
        assert parse_date(s) == date(2026, 9, 28)


def test_reimport_idempotent_and_edited_day_replaces(cfg):
    store = EventStore()
    store.append([ev("consent_recorded", date(2026, 9, 1), {"granted": True, "scope": ["all"]}, "coach")])
    first = parse_files(CID, {"n.csv": NUTRITION.encode()})
    assert store.append(first.events)[0] == 2
    assert store.append(parse_files(CID, {"n.csv": NUTRITION.encode()}).events) == (0, 2)
    edited = NUTRITION.replace("2026-09-29,Breakfast,600.0", "2026-09-29,Breakfast,900.0")
    store.append(parse_files(CID, {"n.csv": edited.encode()}).events)
    st = build_state(store.read(CID), date(2026, 9, 30), cfg)
    assert st.avg_intake_kcal == round((2250 + 900) / 2)  # one value per day, latest wins


def test_mfp_logs_enable_maintenance_estimate(cfg):
    as_of = date(2026, 9, 30)
    rows_n, rows_w = ["Date,Meal,Calories,Fat (g),Carbohydrates (g),Protein (g)"], ["Date,Weight"]
    for i in range(21):
        d = as_of - timedelta(days=i)
        rows_n.append(f"{d},Dinner,2200,60,200,200")
        rows_w.append(f"{d},{200 + i / 7:.2f}")  # losing 1 lb/week
    res = parse_files(CID, {"n.csv": "\n".join(rows_n).encode(), "w.csv": "\n".join(rows_w).encode()})
    st = build_state(res.events, as_of, cfg)
    assert st.maintenance and st.maintenance.kcal == 2700  # 2200 + 1 lb x 3500 / 7


def test_cli_import_mfp(tmp_path, cfg):
    from typer.testing import CliRunner
    from ingest.cli import app
    db = tmp_path / "c.db"
    EventStore(db).append([ev("consent_recorded", date(2026, 9, 1), {"granted": True, "scope": ["all"]}, "coach")])
    f = tmp_path / "Nutrition-Summary.csv"
    f.write_text(NUTRITION)
    r = CliRunner().invoke(app, ["import-mfp", CID, str(f), "--db", str(db)])
    assert r.exit_code == 0 and json.loads(r.output)["inserted"] == 2
    r = CliRunner().invoke(app, ["import-mfp", "NOBODY", str(f), "--db", str(db)])
    assert r.exit_code == 1


def test_dashboard_mfp_upload(tmp_path, cfg):
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    from ingest.onboarding import load_onboarding_config, onboard
    from tests.test_m6_import_onboarding import SAMPLE
    data = tmp_path / "data"
    (data / "syn").mkdir(parents=True)
    onboard(EventStore(data / "syn" / "coach.db"), load_onboarding_config(SAMPLE / "onboarding.json"), cfg,
            base_dir=SAMPLE)
    client = TestClient(create_app(data, cfg))
    # dates just after the sample's imported history, before the page's as_of date
    nutrition = NUTRITION.replace("2026-09-28", "2026-10-01").replace("2026-09-29", "2026-10-02")
    r = client.post("/client/SYN-CUT-01/import-mfp", data={"as_of": "2026-10-03", "unit": "lb"},
                    files=[("files", ("export.zip", _zip({"Nutrition-Summary.csv": nutrition})))],
                    follow_redirects=True)
    assert "MyFitnessPal import: 2 new" in r.text and "Food logs" in r.text and "2250" in r.text
    r = client.post("/client/SYN-CUT-01/import-mfp", data={"as_of": "2026-10-03"},
                    files=[("files", ("evil.sh", b"x"))], follow_redirects=True)
    assert "not a MyFitnessPal report file" in r.text
