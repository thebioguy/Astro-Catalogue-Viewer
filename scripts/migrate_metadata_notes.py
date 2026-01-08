#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, Tuple


def _default_metadata_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Preferences" / "AstroCatalogueViewer" / "Astro Catalogue Viewer" / "metadata"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "AstroCatalogueViewer" / "Astro Catalogue Viewer" / "metadata"
        return home / "AppData" / "Roaming" / "AstroCatalogueViewer" / "Astro Catalogue Viewer" / "metadata"
    return home / ".config" / "AstroCatalogueViewer" / "Astro Catalogue Viewer" / "metadata"


def _load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_notes(payload: Dict) -> Dict[Tuple[str, str], Dict[str, object]]:
    notes: Dict[Tuple[str, str], Dict[str, object]] = {}
    for catalog_name, catalog in payload.items():
        if not isinstance(catalog, dict):
            continue
        for object_id, entry in catalog.items():
            if not isinstance(entry, dict):
                continue
            entry_notes = {}
            if "notes" in entry:
                entry_notes["notes"] = entry["notes"]
            if "image_notes" in entry:
                entry_notes["image_notes"] = entry["image_notes"]
            if entry_notes:
                notes[(str(catalog_name), str(object_id))] = entry_notes
    return notes


def _apply_notes(payload: Dict, notes: Dict[Tuple[str, str], Dict[str, object]]) -> bool:
    changed = False
    for (catalog_name, object_id), entry_notes in notes.items():
        catalog = payload.get(catalog_name)
        if not isinstance(catalog, dict):
            continue
        entry = catalog.get(object_id)
        if not isinstance(entry, dict):
            continue
        if "notes" in entry_notes and "notes" not in entry:
            entry["notes"] = entry_notes["notes"]
            changed = True
        if "image_notes" in entry_notes and "image_notes" not in entry:
            entry["image_notes"] = entry_notes["image_notes"]
            changed = True
    return changed


def _bundle_metadata_dir(app_bundle: Path) -> Path:
    return app_bundle / "Contents" / "Resources" / "data"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate notes/image_notes from an older app bundle into user metadata files.",
    )
    parser.add_argument(
        "--app-bundle",
        required=True,
        help="Path to the old 'Astro Catalogue Viewer.app' bundle.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Destination metadata folder (defaults to user config metadata directory).",
    )
    args = parser.parse_args()

    app_bundle = Path(args.app_bundle).expanduser()
    if app_bundle.name != "Astro Catalogue Viewer.app":
        print("Expected an app bundle named 'Astro Catalogue Viewer.app'.", file=sys.stderr)
    if not app_bundle.exists():
        raise SystemExit(f"App bundle not found: {app_bundle}")

    source_dir = _bundle_metadata_dir(app_bundle)
    if not source_dir.exists():
        raise SystemExit(f"No metadata found in app bundle: {source_dir}")

    dest_dir = Path(args.dest).expanduser() if args.dest else _default_metadata_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_files = sorted(source_dir.glob("*_metadata.json"))
    if not source_files:
        raise SystemExit("No metadata files found in app bundle.")

    migrated = 0
    for source_path in source_files:
        dest_path = dest_dir / source_path.name
        if not dest_path.exists():
            dest_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
        source_payload = _load_json(source_path)
        dest_payload = _load_json(dest_path)
        notes = _extract_notes(source_payload)
        if not notes:
            continue
        if _apply_notes(dest_payload, notes):
            dest_path.write_text(json.dumps(dest_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            migrated += 1

    print(f"Migration complete. Updated {migrated} metadata file(s) in {dest_dir}.")


if __name__ == "__main__":
    main()
