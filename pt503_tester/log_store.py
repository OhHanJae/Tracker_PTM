"""Structured session logging for PT503 communication."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class LogEntry:
    timestamp: str
    direction: str
    hex_data: str
    description: str
    elapsed_ms: str = ""
    checksum: str = ""


class LogStore:
    """Keep UI records and persist every event as UTF-8 JSON Lines."""

    def __init__(self) -> None:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        self.log_dir = base / "OSRND" / "PT503Tester" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        self.session_path = self.log_dir / f"pt503_session_{stamp}.jsonl"
        self.entries: list[LogEntry] = []

    def append(self, entry: LogEntry) -> None:
        self.entries.append(entry)
        with self.session_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def clear_memory(self) -> None:
        # The session file is intentionally retained as an audit trail.
        self.entries.clear()

    def export_csv(self, path: str | Path) -> None:
        target = Path(path)
        with target.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "timestamp",
                    "direction",
                    "hex_data",
                    "description",
                    "elapsed_ms",
                    "checksum",
                ),
            )
            writer.writeheader()
            for entry in self.entries:
                writer.writerow(asdict(entry))
