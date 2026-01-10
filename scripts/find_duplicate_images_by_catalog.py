#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List

CATALOG_PREFIX = {
    "Messier": "M",
    "NGC": "NGC",
    "IC": "IC",
    "Caldwell": "C",
}


def _extract_object_ids(name: str) -> List[str]:
    pattern = re.compile(r"(NGC|IC|M|(?<!I)(?<!NG)C)[\s_-]*0*(\d{1,5})(?!\d)", re.IGNORECASE)
    ids = []
    for match in pattern.finditer(name):
        prefix, number = match.groups()
        ids.append(f"{prefix.upper()}{int(number)}")
    return sorted(set(ids))


def _iter_files(paths: Iterable[Path], extensions: Iterable[str]) -> Iterable[Path]:
    exts = {ext.lower() for ext in extensions}
    for root in paths:
        if not root.exists():
            continue
        for base, _, files in os.walk(root):
            for name in files:
                suffix = Path(name).suffix.lower()
                if suffix not in exts:
                    continue
                yield Path(base) / name


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _catalog_dirs(config: Dict) -> Dict[str, List[Path]]:
    mapping: Dict[str, List[Path]] = {}
    for catalog in config.get("catalogs", []):
        name = catalog.get("name")
        if name not in CATALOG_PREFIX:
            continue
        image_dirs = catalog.get("image_dirs", [])
        if not image_dirs:
            continue
        paths = []
        for image_dir in image_dirs:
            path = Path(image_dir).expanduser()
            if not path.is_absolute():
                path = (PROJECT_ROOT / path).resolve()
            paths.append(path)
        mapping[name] = paths
    return mapping


def _format_report(groups: List[Dict[str, object]]) -> str:
    lines = []
    total_groups = len(groups)
    total_files = sum(len(group["files"]) for group in groups)
    lines.append(f"Duplicate groups: {total_groups}")
    lines.append(f"Duplicate files: {total_files}")
    lines.append("")
    for group in groups:
        lines.append(f"Catalog: {group['catalog']}")
        lines.append(f"SHA-256: {group['hash']}")
        if group["common_ids"]:
            lines.append(f"Common IDs: {', '.join(group['common_ids'])}")
        else:
            lines.append("Common IDs: none")
        for item in group["files"]:
            line = f"  - {item['path']}"
            if item["ids"]:
                line += f" ({', '.join(item['ids'])})"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Find duplicate images within each catalog folder by SHA-256.")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--extensions", default=".jpg,.jpeg,.png,.tif,.tiff,.webp,.bmp", help="Comma-separated extensions")
    parser.add_argument("--output", required=True, help="Output report file path")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    extensions = [ext.strip() for ext in args.extensions.split(",") if ext.strip()]

    groups: List[Dict[str, object]] = []
    for catalog_name, dirs in _catalog_dirs(config).items():
        hashes: Dict[str, List[Path]] = {}
        for path in _iter_files(dirs, extensions):
            digest = _hash_file(path)
            hashes.setdefault(digest, []).append(path)
        for digest, file_paths in hashes.items():
            if len(file_paths) <= 1:
                continue
            file_items = []
            id_sets = []
            for path in file_paths:
                ids = _extract_object_ids(path.stem)
                file_items.append({"path": str(path), "ids": ids})
                id_sets.append(set(ids))
            common_ids: List[str] = []
            if all(id_sets):
                intersection = set.intersection(*id_sets)
                common_ids = sorted(intersection)
            if not common_ids:
                continue
            groups.append(
                {
                    "catalog": catalog_name,
                    "hash": digest,
                    "files": file_items,
                    "common_ids": common_ids,
                }
            )

    groups = sorted(groups, key=lambda g: (g["catalog"], -len(g["files"]), g["hash"]))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = _format_report(groups)
    output.write_text(report, encoding="utf-8")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "extensions": extensions,
        "groups": groups,
    }
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(PROJECT_ROOT / "app"))
    from catalog import PROJECT_ROOT as PROJECT_ROOT  # type: ignore
    main()
