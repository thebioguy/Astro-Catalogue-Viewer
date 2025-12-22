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

            entry = {
                "name": common,
                "type": obj_type,
                "distance_ly": None,
                "discoverer": None,
                "discovery_year": None,
                "best_months": None,
                "description": description,
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
