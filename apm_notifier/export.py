from __future__ import annotations

import csv
import json
from pathlib import Path
import sqlite3


EXPORT_COLUMNS = (
    "company",
    "title",
    "normalized_title_category",
    "location",
    "remote_status",
    "term_start",
    "term_length",
    "posting_url",
    "apply_url",
    "req_id",
    "posted_date",
    "close_date",
    "source",
    "description_hash",
    "decision",
    "reasons",
    "matched_quotes",
    "priority_location",
    "company_priority",
    "first_seen",
    "last_seen",
    "status",
)


def _record(row: sqlite3.Row, include_description: bool) -> dict[str, object]:
    record: dict[str, object] = {column: row[column] for column in EXPORT_COLUMNS}
    record["reasons"] = json.loads(row["reasons"] or "[]")
    record["matched_quotes"] = json.loads(row["matched_quotes"] or "[]")
    record["priority_location"] = bool(row["priority_location"])
    if include_description:
        record["description_text"] = row["description_text"]
    return record


def export_matches(rows: tuple[sqlite3.Row, ...], directory: Path, stem: str = "job_matches") -> tuple[Path, Path]:
    """Write the open ACCEPT/REVIEW postings to CSV (one line per posting) and JSON (with descriptions)."""
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{stem}.csv"
    json_path = directory / f"{stem}.json"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            record = _record(row, include_description=False)
            record["reasons"] = " | ".join(record["reasons"])  # type: ignore[arg-type]
            record["matched_quotes"] = " | ".join(record["matched_quotes"])  # type: ignore[arg-type]
            writer.writerow(record)
    json_path.write_text(
        json.dumps([_record(row, include_description=True) for row in rows], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return csv_path, json_path
