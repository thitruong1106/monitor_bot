"""Append-only log of every alert sent, kept to the last N lines (alerts.jsonl)."""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ALERTS_FILE = os.environ.get("ALERTS_FILE", "alerts.jsonl")
MAX_RECORDS = 500


def make_record(alert: dict, retailer_name: str, ts: datetime | None = None) -> dict:
    """Flatten one alert dict into the row shape stored in alerts.jsonl."""
    when = ts or datetime.now(timezone.utc)
    return {
        "time": when.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "retailer": retailer_name,
        "kind": alert.get("alert_type", "UNKNOWN"),
        "title": alert.get("title", "Unknown Product"),
        "price": alert.get("price"),
        "url": alert.get("product_url", ""),
    }


def _resolve(path: str | None) -> str:
    # Looked up at call time so tests (and ALERTS_FILE env) can redirect it.
    return path or ALERTS_FILE


def read_records(path: str | None = None) -> list[dict]:
    """Read existing rows; silently skip blank or malformed lines."""
    path = _resolve(path)
    if not os.path.exists(path):
        return []
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line in %s", path)
    return records


def append_records(new_records: list[dict], path: str | None = None, keep: int = MAX_RECORDS) -> list[dict]:
    """
    Append rows to the log and trim it to the most recent `keep` rows.
    Returns the full list of rows now on disk.
    """
    path = _resolve(path)
    if not new_records:
        return read_records(path)
    records = read_records(path) + list(new_records)
    if keep and len(records) > keep:
        records = records[-keep:]
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")
    logger.info("Logged %d alert(s) to %s (%d rows kept)", len(new_records), path, len(records))
    return records
