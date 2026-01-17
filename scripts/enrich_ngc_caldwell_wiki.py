#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
OPENNGC_PATH = ROOT / "data" / "openngc" / "NGC.csv"
ADDENDUM_PATH = ROOT / "data" / "openngc" / "addendum.csv"
NGC_META_PATH = ROOT / "data" / "ngc_metadata.json"
IC_META_PATH = ROOT / "data" / "ic_metadata.json"
CALDWELL_META_PATH = ROOT / "data" / "caldwell_metadata.json"
WIKI_API = "https://en.wikipedia.org/w/api.php"

MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]

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


@dataclass
class WikiInfo:
    title: str
    extract: str
    fullurl: str
    thumbnail: Optional[str] = None


@dataclass
class OpenNGCEntry:
    name: Optional[str] = None
    obj_type: Optional[str] = None
    description: Optional[str] = None
    ra_hours: Optional[float] = None
    dec_deg: Optional[float] = None


def _fetch_json(url: str, retries: int = 4) -> Dict:
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            [
                "curl",
                "-sL",
                "--retry",
                "4",
                "--retry-delay",
                "1",
                "-H",
                "User-Agent: AstroCatalogueViewer/1.0",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = result.stdout.strip()
        if payload:
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                pass
        time.sleep(1.5 * attempt)
    raise RuntimeError("Failed to fetch valid JSON from Wikipedia.")


def _fetch_wiki_batch(titles: List[str]) -> Dict[str, WikiInfo]:
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages|extracts|info",
        "redirects": "1",
        "titles": "|".join(titles),
        "exintro": "1",
        "explaintext": "1",
        "exsentences": "2",
        "piprop": "thumbnail",
        "pithumbsize": "640",
        "inprop": "url",
        "formatversion": "2",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    data = _fetch_json(url)
    query = data.get("query", {})
    normalized = {row.get("from"): row.get("to") for row in query.get("normalized", [])}
    redirects = {row.get("from"): row.get("to") for row in query.get("redirects", [])}
    pages = query.get("pages", [])
    pages_by_title = {page.get("title"): page for page in pages if not page.get("missing")}

    info_by_title: Dict[str, WikiInfo] = {}
    for title in titles:
        resolved = redirects.get(normalized.get(title, title), normalized.get(title, title))
        page = pages_by_title.get(resolved)
        if not page:
            continue
        extract = (page.get("extract") or "").strip()
        if not extract:
            continue
        info_by_title[title] = WikiInfo(
            title=page.get("title", resolved),
            extract=extract,
            fullurl=page.get("fullurl", ""),
            thumbnail=(page.get("thumbnail") or {}).get("source"),
        )
    return info_by_title


def _fetch_wiki_info(titles: Iterable[str], batch_size: int = 50) -> Dict[str, WikiInfo]:
    results: Dict[str, WikiInfo] = {}
    batch: List[str] = []
    for title in titles:
        if title in results:
            continue
        batch.append(title)
        if len(batch) >= batch_size:
            results.update(_fetch_wiki_batch(batch))
            batch.clear()
            time.sleep(0.2)
    if batch:
        results.update(_fetch_wiki_batch(batch))
    return results


def _parse_ra(value: str | None) -> Optional[float]:
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


def _parse_dec(value: str | None) -> Optional[float]:
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


def _best_months_from_ra(ra_hours: Optional[float]) -> Optional[str]:
    if ra_hours is None:
        return None
    month_index = int((ra_hours / 2 + 9) % 12)
    if month_index == 0:
        month_index = 12
    idx = month_index - 1
    prev_month = MONTHS[(idx - 1) % 12]
    curr_month = MONTHS[idx]
    next_month = MONTHS[(idx + 1) % 12]
    return f"{prev_month}{curr_month}{next_month}"


def _normalize_object_id(value: str) -> str:
    match = re.match(r"^(NGC|IC)\s*0*(\d+)$", value.strip(), re.IGNORECASE)
    if not match:
        return value.replace(" ", "").upper()
    prefix = match.group(1).upper()
    number = int(match.group(2))
    return f"{prefix}{number}"


def _title_from_object_id(object_id: str) -> Optional[str]:
    match = re.match(r"^(NGC|IC)\s*0*(\d+)([A-Z]?)$", object_id.strip(), re.IGNORECASE)
    if not match:
        return None
    prefix = match.group(1).upper()
    number = int(match.group(2))
    suffix = match.group(3)
    suffix = suffix.upper() if suffix else ""
    return f"{prefix} {number}{suffix}"


def _load_openngc_entries() -> Dict[str, OpenNGCEntry]:
    entries: Dict[str, OpenNGCEntry] = {}
    if not OPENNGC_PATH.exists():
        return entries
    with OPENNGC_PATH.open(newline="", encoding="utf-8", errors="replace") as handle:
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

            common = (row.get("Common names") or "").strip() or None
            if common:
                common = re.split(r"[;,/]", common)[0].strip()

            type_code = (row.get("Type") or "").strip()
            obj_type = TYPE_MAP.get(type_code, type_code) or None

            description = (row.get("OpenNGC notes") or "").strip()
            if not description:
                description = (row.get("NED notes") or "").strip()

            ra_hours = _parse_ra(row.get("RA"))
            dec_deg = _parse_dec(row.get("Dec"))

            entries[object_id] = OpenNGCEntry(
                name=common,
                obj_type=obj_type,
                description=description or None,
                ra_hours=ra_hours,
                dec_deg=dec_deg,
            )
    return entries


def _load_caldwell_mappings() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not OPENNGC_PATH.exists():
        return mapping
    with OPENNGC_PATH.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            identifiers = (row.get("Identifiers") or "").replace(",", ";")
            for raw in identifiers.split(";"):
                token = raw.strip()
                if not token:
                    continue
                match = re.match(r"^C\s*0*(\d+)$", token, re.IGNORECASE)
                if not match:
                    continue
                num = int(match.group(1))
                mapping[f"C{num}"] = _normalize_object_id(name)
    return mapping


def _load_caldwell_addendum() -> Dict[str, OpenNGCEntry]:
    entries: Dict[str, OpenNGCEntry] = {}
    if not ADDENDUM_PATH.exists():
        return entries
    with ADDENDUM_PATH.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            match = re.match(r"^C\s*0*(\d+)$", name, re.IGNORECASE)
            if not match:
                continue
            num = int(match.group(1))
            object_id = f"C{num}"
            common = (row.get("Common names") or "").strip() or None
            if common:
                common = re.split(r"[;,/]", common)[0].strip()
            type_code = (row.get("Type") or "").strip()
            obj_type = TYPE_MAP.get(type_code, type_code) or None
            description = (row.get("OpenNGC notes") or "").strip()
            if not description:
                description = (row.get("NED notes") or "").strip()
            ra_hours = _parse_ra(row.get("RA"))
            dec_deg = _parse_dec(row.get("Dec"))
            entries[object_id] = OpenNGCEntry(
                name=common,
                obj_type=obj_type,
                description=description or None,
                ra_hours=ra_hours,
                dec_deg=dec_deg,
            )
    return entries


def _caldwell_addendum_overrides() -> Dict[str, str]:
    return {
        "C9": "Cave Nebula",
        "C14": "Double Cluster",
        "C41": "Hyades (star cluster)",
        "C99": "Coalsack Nebula",
    }


def _ensure_external_link(meta: Dict, default_title: Optional[str]) -> None:
    if meta.get("external_link"):
        return
    if not default_title:
        return
    slug = default_title.replace(" ", "_")
    meta["external_link"] = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(slug)}"


def _update_metadata_with_wiki(
    entries: Dict[str, Dict],
    title_map: Dict[str, str],
    wiki_info: Dict[str, WikiInfo],
) -> int:
    updated = 0
    for object_id, meta in entries.items():
        title = title_map.get(object_id)
        if not title:
            continue
        info = wiki_info.get(title)
        if not info:
            _ensure_external_link(meta, title)
            continue
        meta["description"] = info.extract
        if info.fullurl:
            meta["external_link"] = info.fullurl
        if info.thumbnail:
            meta["wiki_thumbnail"] = info.thumbnail
        updated += 1
    return updated


def _load_metadata(path: Path, key: str) -> Dict[str, Dict]:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    entries = data.get(key, {})
    if not isinstance(entries, dict):
        raise SystemExit(f"{path} does not contain {key} metadata.")
    return entries


def main() -> None:
    if not OPENNGC_PATH.exists():
        raise SystemExit(f"Missing OpenNGC CSV at {OPENNGC_PATH}")
    if not NGC_META_PATH.exists():
        raise SystemExit(f"Missing metadata: {NGC_META_PATH}")
    if not IC_META_PATH.exists():
        raise SystemExit(f"Missing metadata: {IC_META_PATH}")

    openngc_entries = _load_openngc_entries()
    caldwell_mapping = _load_caldwell_mappings()
    caldwell_addendum = _load_caldwell_addendum()

    ngc_entries = _load_metadata(NGC_META_PATH, "NGC")
    ic_entries = _load_metadata(IC_META_PATH, "IC")

    ngc_titles = {}
    for object_id in ngc_entries:
        title = _title_from_object_id(object_id)
        if title:
            ngc_titles[object_id] = title
    ic_titles = {}
    for object_id in ic_entries:
        title = _title_from_object_id(object_id)
        if title:
            ic_titles[object_id] = title

    print(f"Fetching Wikipedia summaries for {len(ngc_titles)} NGC entries...")
    ngc_wiki = _fetch_wiki_info(sorted(set(ngc_titles.values())))
    updated_ngc = _update_metadata_with_wiki(ngc_entries, ngc_titles, ngc_wiki)
    print(f"NGC entries updated with Wikipedia data: {updated_ngc}")

    missing_name_titles: Dict[str, str] = {}
    for object_id, meta in ngc_entries.items():
        if meta.get("description"):
            continue
        name = (meta.get("name") or "").strip()
        if not name:
            continue
        if name.lower() == object_id.lower():
            continue
        missing_name_titles[object_id] = name

    if missing_name_titles:
        print(f"Fetching Wikipedia summaries for {len(missing_name_titles)} NGC common names...")
        name_wiki = _fetch_wiki_info(sorted(set(missing_name_titles.values())))
        updated_common = _update_metadata_with_wiki(ngc_entries, missing_name_titles, name_wiki)
        print(f"NGC entries updated via common names: {updated_common}")

    caldwell_ic_ids = [obj for obj in caldwell_mapping.values() if obj.startswith("IC")]
    caldwell_ic_titles = {obj: ic_titles.get(obj) for obj in caldwell_ic_ids if ic_titles.get(obj)}
    if caldwell_ic_titles:
        print(f"Fetching Wikipedia summaries for {len(caldwell_ic_titles)} IC entries used by Caldwell...")
        ic_wiki = _fetch_wiki_info(sorted(set(caldwell_ic_titles.values())))
        _update_metadata_with_wiki(ic_entries, caldwell_ic_titles, ic_wiki)
    else:
        ic_wiki = {}

    ngc_payload = {"NGC": ngc_entries}
    NGC_META_PATH.write_text(json.dumps(ngc_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    ic_payload = {"IC": ic_entries}
    IC_META_PATH.write_text(json.dumps(ic_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    caldwell_entries: Dict[str, Dict] = {}
    for caldwell_id, base_id in caldwell_mapping.items():
        meta: Dict[str, Optional[object]] = {
            "name": None,
            "type": None,
            "distance_ly": None,
            "discoverer": None,
            "discovery_year": None,
            "best_months": None,
            "description": None,
            "ra_hours": None,
            "dec_deg": None,
            "external_link": None,
        }
        if base_id.startswith("NGC"):
            base_meta = ngc_entries.get(base_id, {})
        else:
            base_meta = ic_entries.get(base_id, {})

        if base_meta:
            meta.update(
                {
                    "name": base_meta.get("name"),
                    "type": base_meta.get("type"),
                    "distance_ly": base_meta.get("distance_ly"),
                    "discoverer": base_meta.get("discoverer"),
                    "discovery_year": base_meta.get("discovery_year"),
                    "best_months": base_meta.get("best_months"),
                    "description": base_meta.get("description"),
                    "ra_hours": base_meta.get("ra_hours"),
                    "dec_deg": base_meta.get("dec_deg"),
                    "external_link": base_meta.get("external_link"),
                }
            )
            if not meta.get("best_months"):
                meta["best_months"] = _best_months_from_ra(meta.get("ra_hours"))
        else:
            openngc_entry = openngc_entries.get(base_id)
            if openngc_entry:
                meta.update(
                    {
                        "name": openngc_entry.name,
                        "type": openngc_entry.obj_type,
                        "description": openngc_entry.description,
                        "ra_hours": openngc_entry.ra_hours,
                        "dec_deg": openngc_entry.dec_deg,
                    }
                )
                meta["best_months"] = _best_months_from_ra(openngc_entry.ra_hours)

        caldwell_entries[caldwell_id] = meta

    if caldwell_addendum:
        addendum_titles = []
        addendum_overrides = _caldwell_addendum_overrides()
        for caldwell_id, entry in caldwell_addendum.items():
            override_title = addendum_overrides.get(caldwell_id)
            meta = {
                "name": entry.name or override_title or "",
                "type": entry.obj_type,
                "distance_ly": None,
                "discoverer": None,
                "discovery_year": None,
                "best_months": _best_months_from_ra(entry.ra_hours),
                "description": entry.description or "",
                "ra_hours": entry.ra_hours,
                "dec_deg": entry.dec_deg,
                "external_link": None,
            }
            caldwell_entries[caldwell_id] = meta
            if override_title:
                addendum_titles.append(override_title)
            elif entry.name:
                addendum_titles.append(entry.name)

        if addendum_titles:
            print(f"Fetching Wikipedia summaries for {len(addendum_titles)} Caldwell addendum entries...")
            addendum_wiki = _fetch_wiki_info(addendum_titles)
            for caldwell_id, entry in caldwell_addendum.items():
                override_title = addendum_overrides.get(caldwell_id)
                title = override_title or entry.name
                if not title:
                    continue
                info = addendum_wiki.get(title)
                if not info:
                    continue
                meta = caldwell_entries.get(caldwell_id)
                if not meta:
                    continue
                meta["description"] = info.extract
                if info.fullurl:
                    meta["external_link"] = info.fullurl
                if info.thumbnail:
                    meta["wiki_thumbnail"] = info.thumbnail

    ordered = dict(sorted(caldwell_entries.items(), key=lambda item: int(item[0][1:])))
    CALDWELL_META_PATH.write_text(
        json.dumps({"Caldwell": ordered}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Caldwell entries written: {len(ordered)}")


if __name__ == "__main__":
    main()
