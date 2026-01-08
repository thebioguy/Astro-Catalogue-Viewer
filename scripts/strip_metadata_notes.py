#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


def _metadata_files() -> Iterable[Path]:
    return sorted(DATA_DIR.glob("*_metadata.json"))


def _strip_notes(payload: dict) -> bool:
    changed = False
    for catalog in payload.values():
        if not isinstance(catalog, dict):
            continue
        for entry in catalog.values():
            if not isinstance(entry, dict):
                continue
            if "notes" in entry:
                entry.pop("notes", None)
                changed = True
            if "image_notes" in entry:
                entry.pop("image_notes", None)
                changed = True
    return changed


def main() -> None:
    for path in _metadata_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        if _strip_notes(data):
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
