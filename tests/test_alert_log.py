"""Tests for alert_log.py: the alerts.jsonl price history (fix 5)."""
import json
from datetime import datetime, timezone

import alert_log


def _alert(url="https://shop.com/products/op09", title="OP09 Booster Box", price="$69.95", kind="BACK_IN_STOCK"):
    return {"product_url": url, "title": title, "price": price, "alert_type": kind}


def test_make_record_has_exactly_the_requested_fields():
    ts = datetime(2026, 11, 2, 8, 5, 0, tzinfo=timezone.utc)
    rec = alert_log.make_record(_alert(), "Gameology", ts=ts)
    assert rec == {
        "time": "2026-11-02T08:05:00Z",
        "retailer": "Gameology",
        "kind": "BACK_IN_STOCK",
        "title": "OP09 Booster Box",
        "price": "$69.95",
        "url": "https://shop.com/products/op09",
    }


def test_make_record_keeps_null_price():
    rec = alert_log.make_record(_alert(price=None), "Kaika")
    assert rec["price"] is None


def test_append_creates_file_and_writes_one_json_object_per_line(tmp_path):
    path = tmp_path / "alerts.jsonl"
    alert_log.append_records([alert_log.make_record(_alert(), "A"), alert_log.make_record(_alert(title="B"), "B")], path=str(path))
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[1])["title"] == "B"


def test_append_is_cumulative_across_runs(tmp_path):
    path = str(tmp_path / "alerts.jsonl")
    alert_log.append_records([alert_log.make_record(_alert(title="run1"), "A")], path=path)
    alert_log.append_records([alert_log.make_record(_alert(title="run2"), "A")], path=path)
    titles = [r["title"] for r in alert_log.read_records(path)]
    assert titles == ["run1", "run2"]


def test_append_trims_to_last_500(tmp_path):
    path = str(tmp_path / "alerts.jsonl")
    first = [alert_log.make_record(_alert(title=f"old-{i}"), "A") for i in range(495)]
    alert_log.append_records(first, path=path)
    second = [alert_log.make_record(_alert(title=f"new-{i}"), "A") for i in range(10)]
    kept = alert_log.append_records(second, path=path)
    assert len(kept) == 500
    on_disk = alert_log.read_records(path)
    assert len(on_disk) == 500
    assert on_disk[0]["title"] == "old-5"       # the five oldest rows fell off
    assert on_disk[-1]["title"] == "new-9"      # newest row is last


def test_append_with_nothing_new_leaves_file_untouched(tmp_path):
    path = tmp_path / "alerts.jsonl"
    path.write_text('{"title": "keep"}\n', encoding="utf-8")
    alert_log.append_records([], path=str(path))
    assert path.read_text(encoding="utf-8") == '{"title": "keep"}\n'


def test_read_skips_blank_and_malformed_lines(tmp_path):
    path = tmp_path / "alerts.jsonl"
    path.write_text('{"title": "ok"}\n\nnot json\n{"title": "ok2"}\n', encoding="utf-8")
    titles = [r["title"] for r in alert_log.read_records(str(path))]
    assert titles == ["ok", "ok2"]
