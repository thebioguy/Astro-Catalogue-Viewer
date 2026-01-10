#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List

CATALOG_PREFIX = {
    "Messier": "M",
    "NGC": "NGC",
    "IC": "IC",
    "Caldwell": "C",
}


def _extract_object_ids(stem: str) -> List[str]:
    pattern = re.compile(r"(NGC|IC|M|(?<!I)(?<!NG)C)[\s_-]*0*(\d{1,5})(?!\d)", re.IGNORECASE)
    ids = []
    for match in pattern.finditer(stem):
        prefix, number = match.groups()
        ids.append(f"{prefix.upper()}{int(number)}")
    return ids


def _iter_files(root: Path, extensions: Iterable[str]) -> Iterable[Path]:
    exts = {ext.lower() for ext in extensions}
    if not root.exists():
        return
    for base, _, files in os.walk(root):
        for name in files:
            suffix = Path(name).suffix.lower()
            if suffix not in exts:
                continue
            yield Path(base) / name


def _catalog_target_dirs(config: Dict) -> Dict[str, Path]:
    mapping: Dict[str, Path] = {}
    for catalog in config.get("catalogs", []):
        name = catalog.get("name")
        if name not in CATALOG_PREFIX:
            continue
        image_dirs = catalog.get("image_dirs", [])
        if not image_dirs:
            continue
        target = Path(image_dirs[0]).expanduser()
        if not target.is_absolute():
            target = (PROJECT_ROOT / target).resolve()
        mapping[name] = target
    return mapping


def _resolve_master(config: Dict) -> Path | None:
    master_dir = (config.get("master_image_dir") or "").strip()
    if not master_dir:
        return None
    root = Path(master_dir).expanduser()
    if not root.is_absolute():
        root = (PROJECT_ROOT / root).resolve()
    return root


def _pick_catalog(object_ids: List[str]) -> str | None:
    priority = ["M", "NGC", "IC", "C"]
    for prefix in priority:
        for object_id in object_ids:
            if object_id.startswith(prefix):
                return prefix
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Sort master images into catalog folders based on filenames.")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--extensions", default=".jpg,.jpeg,.png,.tif,.tiff,.webp,.bmp", help="Comma-separated extensions")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    extensions = [ext.strip() for ext in args.extensions.split(",") if ext.strip()]

    master_root = _resolve_master(config)
    if master_root is None or not master_root.exists():
        print("No master image folder configured.")
        return

    catalog_dirs = _catalog_target_dirs(config)
    prefix_to_catalog = {v: k for k, v in CATALOG_PREFIX.items()}
    moved = 0
    skipped = 0

    for path in _iter_files(master_root, extensions):
        object_ids = _extract_object_ids(path.stem.upper())
        catalog_prefix = _pick_catalog(object_ids)
        if not catalog_prefix:
            skipped += 1
            continue
        catalog_name = prefix_to_catalog.get(catalog_prefix)
        if catalog_name is None:
            skipped += 1
            continue
        target_dir = catalog_dirs.get(catalog_name)
        if target_dir is None:
            skipped += 1
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / path.name
        if path.parent.resolve() == target_dir.resolve():
            skipped += 1
            continue
        counter = 1
        while target_path.exists():
            target_path = target_dir / f"{path.stem}-{counter}{path.suffix}"
            counter += 1
        try:
            shutil.move(str(path), str(target_path))
            moved += 1
        except OSError:
            skipped += 1

    print(f"Moved {moved} file(s). Skipped {skipped} file(s).")


if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(PROJECT_ROOT / "app"))
    from catalog import PROJECT_ROOT as PROJECT_ROOT  # type: ignore
    main()
