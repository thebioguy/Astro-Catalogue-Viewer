#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from pathlib import Path


TYPE_MAP = {
    "*": "Star",
    "**": "Double Star",
    "*Ass": "Association of Stars",
    "OCl": "Open Cluster",
    "GCl": "Globular Cluster",
    "Cl+N": "Cluster + Nebula",
    "G": "Galaxy",
    "GPair": "Galaxy Pair",
    "GTrpl": "Galaxy Triplet",
    "GGroup": "Galaxy Group",
    "PN": "Planetary Nebula",
    "HII": "HII Region",
    "DrkN": "Dark Nebula",
    "EmN": "Emission Nebula",
    "Neb": "Nebula",
    "RfN": "Reflection Nebula",
    "SNR": "Supernova Remnant",
    "Nova": "Nova",
    "NonEx": "Nonexistent Object",
    "Dup": "Duplicate Entry",
    "Other": "Other",
}


def _parse_ra(value: str | None) -> float | None:
    if not value:
        return None
    parts = re.split(r"[:\s]+", value.strip())
    try:
        hours = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
        return hours + minutes / 60.0 + seconds / 3600.0
    except ValueError:
        return None


def _parse_dec(value: str | None) -> float | None:
    if not value:
        return None
    sign = -1.0 if value.strip().startswith("-") else 1.0
    text = value.strip().lstrip("+-")
    parts = re.split(r"[:\s]+", text)
    try:
        deg = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
        return sign * (deg + minutes / 60.0 + seconds / 3600.0)
    except ValueError:
        return None


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    openngc = root / "data" / "openngc" / "NGC.csv"
    if not openngc.exists():
        raise SystemExit(f"Missing OpenNGC CSV at {openngc}")

    ngc: dict[str, dict] = {}
    ic: dict[str, dict] = {}

    with openngc.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            match = re.match(r"^(NGC|IC)\s*0*([0-9]+)$", name, re.IGNORECASE)
            if not match:
                continue
            prefix = match.group(1).upper()
            number = str(int(match.group(2)))
            object_id = f"{prefix}{number}"

            common = (row.get("Common names") or "").strip()
            if common:
                common = re.split(r"[;,/]", common)[0].strip()

            type_code = (row.get("Type") or "").strip()
            obj_type = TYPE_MAP.get(type_code, type_code)

            description = (row.get("OpenNGC notes") or "").strip()
            if not description:
                description = (row.get("NED notes") or "").strip()

            ra_hours = _parse_ra(row.get("RA"))
            dec_deg = _parse_dec(row.get("Dec"))

            entry = {
                "name": common,
                "type": obj_type,
                "distance_ly": None,
                "discoverer": None,
                "discovery_year": None,
                "best_months": None,
                "description": description,
                "ra_hours": ra_hours,
                "dec_deg": dec_deg,
            }

            if prefix == "NGC":
                ngc[object_id] = entry
            else:
                ic[object_id] = entry

    ngc_path = root / "data" / "ngc_metadata.json"
    ic_path = root / "data" / "ic_metadata.json"
    ngc_path.write_text(json.dumps({"NGC": ngc}, indent=2, ensure_ascii=False))
    ic_path.write_text(json.dumps({"IC": ic}, indent=2, ensure_ascii=False))
    print(f"NGC objects: {len(ngc)}")
    print(f"IC objects: {len(ic)}")


if __name__ == "__main__":
    main()
