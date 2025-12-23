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
from typing import Dict, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
NGC_META_PATH = ROOT / "data" / "ngc_metadata.json"
OPENNGC_PATH = ROOT / "data" / "openngc" / "NGC.csv"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class WikiRecord:
    item_id: Optional[str] = None
    discoverer_id: Optional[str] = None
    discovery_year: Optional[int] = None
    distance_ly: Optional[float] = None


@dataclass
class OpenNGCRecord:
    common_name: Optional[str] = None
    ra_hours: Optional[float] = None


def _fetch_json(url: str, retries: int = 4) -> Dict:
    # Use curl to avoid local SSL certificate issues.
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
    raise RuntimeError("Failed to fetch valid JSON from Wikidata.")


def _sparql_query(query: str) -> Dict:
    params = urllib.parse.urlencode({"format": "json", "query": query})
    url = f"{WIKIDATA_ENDPOINT}?{params}"
    return _fetch_json(url)


def _iter_wikidata_rows(object_ids: Iterable[str], batch_size: int = 50) -> Iterable[Dict]:
    ids = []
    for object_id in object_ids:
        match = re.match(r"NGC\s*0*(\d+)", object_id, re.IGNORECASE)
        if not match:
            continue
        ids.append(int(match.group(1)))
    ids.sort()

    for start in range(0, len(ids), batch_size):
        batch = ids[start: start + batch_size]
        values = " ".join(f'\"NGC {num}\"' for num in batch)
        print(f"Fetching Wikidata batch {start // batch_size + 1} / {((len(ids) - 1) // batch_size) + 1}...")
        query = f"""
SELECT ?item ?ngc ?discoverer ?discovery ?distanceAmount ?distanceUnit WHERE {{
  VALUES ?ngc {{ {values} }}
  ?item wdt:P528 ?ngc .
  OPTIONAL {{ ?item wdt:P61 ?discoverer . }}
  OPTIONAL {{ ?item wdt:P575 ?discovery . }}
  OPTIONAL {{
    ?item p:P2583/psv:P2583 ?distanceNode .
    ?distanceNode wikibase:quantityAmount ?distanceAmount ;
                  wikibase:quantityUnit ?distanceUnit .
  }}
}}
"""
        data = _sparql_query(query)
        rows = data.get("results", {}).get("bindings", [])
        for row in rows:
            yield row
        time.sleep(0.2)


def _parse_ngc_code(value: str) -> Optional[str]:
    match = re.search(r"NGC\s*0*(\d+)", value, re.IGNORECASE)
    if not match:
        return None
    return f"NGC{int(match.group(1))}"


def _parse_discovery_year(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = re.match(r"^(\d{4})", value)
    if not match:
        return None
    return int(match.group(1))


def _convert_distance_to_ly(amount: Optional[str], unit: Optional[str]) -> Optional[float]:
    if not amount or not unit:
        return None
    try:
        value = float(amount)
    except ValueError:
        return None
    unit_id = unit.rsplit("/", 1)[-1]
    if unit_id == "Q531":
        return value
    if unit_id == "Q12129":
        return value * 3.26156
    if unit_id == "Q11929860":
        return value * 3261.56
    if unit_id == "Q3773454":
        return value * 3_261_560.0
    return None


def _choose_distance(existing: Optional[float], candidate: Optional[float], unit: Optional[str]) -> Optional[float]:
    if candidate is None:
        return existing
    if existing is None:
        return candidate
    if unit and unit.rsplit("/", 1)[-1] == "Q531":
        return candidate
    return existing


def _load_openngc() -> Dict[str, OpenNGCRecord]:
    records: Dict[str, OpenNGCRecord] = {}
    if not OPENNGC_PATH.exists():
        return records
    with OPENNGC_PATH.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            match = re.match(r"^(NGC)\s*0*([0-9]+)$", name, re.IGNORECASE)
            if not match:
                continue
            object_id = f"NGC{int(match.group(2))}"
            common = (row.get("Common names") or "").strip() or None
            if common:
                common = re.split(r"[;,/]", common)[0].strip()
            ra_hours = _parse_ra_hours((row.get("RA") or "").strip())
            records[object_id] = OpenNGCRecord(common_name=common, ra_hours=ra_hours)
    return records


def _parse_ra_hours(ra_text: str) -> Optional[float]:
    if not ra_text:
        return None
    parts = ra_text.split(":")
    try:
        hours = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
    except ValueError:
        return None
    return hours + minutes / 60.0 + seconds / 3600.0


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


def _build_wiki_index(object_ids: Iterable[str]) -> Dict[str, WikiRecord]:
    wiki: Dict[str, WikiRecord] = {}
    for row in _iter_wikidata_rows(object_ids):
        ngc_value = row.get("ngc", {}).get("value")
        if not ngc_value:
            continue
        object_id = _parse_ngc_code(ngc_value)
        if not object_id:
            continue
        record = wiki.setdefault(object_id, WikiRecord())

        item_uri = row.get("item", {}).get("value")
        if item_uri:
            record.item_id = record.item_id or item_uri.rsplit("/", 1)[-1]

        discoverer_uri = row.get("discoverer", {}).get("value")
        if discoverer_uri:
            record.discoverer_id = record.discoverer_id or discoverer_uri.rsplit("/", 1)[-1]

        discovery_year = _parse_discovery_year(row.get("discovery", {}).get("value"))
        if discovery_year:
            record.discovery_year = record.discovery_year or discovery_year

        distance_amount = row.get("distanceAmount", {}).get("value")
        distance_unit = row.get("distanceUnit", {}).get("value")
        distance_ly = _convert_distance_to_ly(distance_amount, distance_unit)
        record.distance_ly = _choose_distance(record.distance_ly, distance_ly, distance_unit)
    return wiki


def _fetch_labels(qids: Iterable[str]) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    batch: list[str] = []
    for qid in qids:
        if qid and qid not in labels:
            batch.append(qid)
        if len(batch) >= 50:
            labels.update(_fetch_label_batch(batch))
            batch.clear()
    if batch:
        labels.update(_fetch_label_batch(batch))
    return labels


def _fetch_label_batch(qids: Iterable[str]) -> Dict[str, str]:
    ids = "|".join(sorted(set(qids)))
    params = urllib.parse.urlencode(
        {
            "action": "wbgetentities",
            "ids": ids,
            "props": "labels",
            "languages": "en",
            "format": "json",
        }
    )
    url = f"https://www.wikidata.org/w/api.php?{params}"
    data = _fetch_json(url)
    entities = data.get("entities", {})
    labels: Dict[str, str] = {}
    for qid, entity in entities.items():
        label = entity.get("labels", {}).get("en", {}).get("value")
        if label:
            labels[qid] = label
    return labels


def main() -> None:
    if not NGC_META_PATH.exists():
        raise SystemExit(f"Missing metadata: {NGC_META_PATH}")
    data = json.loads(NGC_META_PATH.read_text(encoding="utf-8"))
    entries = data.get("NGC", {})
    if not isinstance(entries, dict):
        raise SystemExit("NGC metadata is not a dictionary.")

    openngc = _load_openngc()
    wiki = _build_wiki_index(entries.keys())

    discoverer_ids = sorted({rec.discoverer_id for rec in wiki.values() if rec.discoverer_id})
    item_ids = sorted({rec.item_id for rec in wiki.values() if rec.item_id})
    discoverer_labels = _fetch_labels(discoverer_ids)
    item_labels = _fetch_labels(item_ids)

    updated = 0
    for object_id, meta in entries.items():
        if not isinstance(meta, dict):
            continue
        openngc_entry = openngc.get(object_id)
        wiki_entry = wiki.get(object_id)

        if not meta.get("name"):
            candidate = None
            if openngc_entry and openngc_entry.common_name:
                candidate = openngc_entry.common_name
            if wiki_entry and wiki_entry.item_id:
                label = item_labels.get(wiki_entry.item_id)
                if label and not re.match(r"^NGC\s*\d+[A-Z]?$", label, re.IGNORECASE):
                    candidate = candidate or label
            if candidate:
                meta["name"] = candidate
                updated += 1

        if not meta.get("discoverer") and wiki_entry and wiki_entry.discoverer_id:
            label = discoverer_labels.get(wiki_entry.discoverer_id)
            if label:
                meta["discoverer"] = label
                updated += 1

        if meta.get("discovery_year") is None and wiki_entry and wiki_entry.discovery_year:
            meta["discovery_year"] = wiki_entry.discovery_year
            updated += 1

        if meta.get("distance_ly") is None and wiki_entry and wiki_entry.distance_ly is not None:
            meta["distance_ly"] = round(wiki_entry.distance_ly, 1)
            updated += 1

        if not meta.get("best_months"):
            best_months = _best_months_from_ra(openngc_entry.ra_hours if openngc_entry else None)
            if best_months:
                meta["best_months"] = best_months
                updated += 1

    NGC_META_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Updated fields: {updated}")
    print(f"Wikidata matches: {len(wiki)}")


if __name__ == "__main__":
    main()
