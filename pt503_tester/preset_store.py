"""Persistent, application-side coordinate notes for device presets."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


class PresetStore:
    """Store coordinates locally because Pelco-D cannot read preset targets."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
            path = base / "OSRND" / "PT503Tester" / "presets.json"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, dict[str, object]]] = {}
        self._load()

    def get(self, address: int, preset: int) -> dict[str, object] | None:
        value = self._data.get(str(address), {}).get(str(preset))
        return dict(value) if value else None

    def set(self, address: int, preset: int, pan: float, tilt: float) -> None:
        by_address = self._data.setdefault(str(address), {})
        by_address[str(preset)] = {
            "pan": round(float(pan) % 360.0, 2),
            "tilt": round(float(tilt), 2),
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._save()

    def delete(self, address: int, preset: int) -> None:
        by_address = self._data.get(str(address), {})
        by_address.pop(str(preset), None)
        if not by_address:
            self._data.pop(str(address), None)
        self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, json.JSONDecodeError):
            # A damaged note file must never prevent control or safety commands.
            self._data = {}

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)
