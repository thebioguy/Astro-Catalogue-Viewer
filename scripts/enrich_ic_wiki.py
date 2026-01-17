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
IC_META_PATH = ROOT / "data" / "ic_metadata.json"
OPENNGC_PATH = ROOT / "data" / "openngc" / "NGC.csv"
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
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


@dataclass
class WikiRecord:
    item_id: Optional[str] = None
    discoverer_id: Optional[str] = None
    discovery_year: Optional[int] = None
    distance_ly: Optional[float] = None


@dataclass
class OpenNGCRecord:
    common_name: Optional[str] = None
    description: Optional[str] = None
    ra_hours: Optional[float] = None
    dec_deg: Optional[float] = None


@dataclass
class WikiInfo:
    title: str
    extract: str
    fullurl: str
    thumbnail: Optional[str] = None


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
    raise RuntimeError("Failed to fetch valid JSON response.")


def _sparql_query(query: str) -> Dict:
    params = urllib.parse.urlencode({"format": "json", "query": query})
    url = f"{WIKIDATA_ENDPOINT}?{params}"
    return _fetch_json(url)


def _iter_wikidata_rows(object_ids: Iterable[str], batch_size: int = 50) -> Iterable[Dict]:
    ids = []
    for object_id in object_ids:
        match = re.match(r"IC\s*0*(\d+)", object_id, re.IGNORECASE)
        if not match:
            continue
        ids.append(int(match.group(1)))
    ids.sort()

    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        values = " ".join(f'"IC {num}"' for num in batch)
        print(f"Fetching Wikidata batch {start // batch_size + 1} / {((len(ids) - 1) // batch_size) + 1}...")
        query = f"""
SELECT ?item ?ic ?discoverer ?discovery ?distanceAmount ?distanceUnit WHERE {{
  VALUES ?ic {{ {values} }}
  ?item wdt:P528 ?ic .
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


def _parse_ic_code(value: str) -> Optional[str]:
    match = re.search(r"IC\s*0*(\d+)", value, re.IGNORECASE)
    if not match:
        return None
    return f"IC{int(match.group(1))}"


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
            match = re.match(r"^(IC)\s*0*([0-9]+)$", name, re.IGNORECASE)
            if not match:
                continue
            object_id = f"IC{int(match.group(2))}"
            common = (row.get("Common names") or "").strip() or None
            if common:
                common = re.split(r"[;,/]", common)[0].strip()
            description = (row.get("OpenNGC notes") or "").strip()
            if not description:
                description = (row.get("NED notes") or "").strip()
            ra_hours = _parse_ra_hours((row.get("RA") or "").strip())
            dec_deg = _parse_dec_deg((row.get("Dec") or "").strip())
            records[object_id] = OpenNGCRecord(
                common_name=common,
                description=description or None,
                ra_hours=ra_hours,
                dec_deg=dec_deg,
            )
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


def _parse_dec_deg(dec_text: str) -> Optional[float]:
    if not dec_text:
        return None
    sign = -1.0 if dec_text.strip().startswith("-") else 1.0
    text = dec_text.strip().lstrip("+-")
    parts = text.split(":")
    try:
        deg = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
    except ValueError:
        return None
    return sign * (deg + minutes / 60.0 + seconds / 3600.0)


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
        ic_value = row.get("ic", {}).get("value")
        if not ic_value:
            continue
        object_id = _parse_ic_code(ic_value)
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


def _title_from_object_id(object_id: str) -> Optional[str]:
    match = re.match(r"^IC\s*0*(\d+)([A-Z]?)$", object_id.strip(), re.IGNORECASE)
    if not match:
        return None
    number = int(match.group(1))
    suffix = match.group(2)
    suffix = suffix.upper() if suffix else ""
    return f"IC {number}{suffix}"


def _fetch_wiki_batch(titles: list[str]) -> Dict[str, WikiInfo]:
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
    batch: list[str] = []
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


def _looks_astronomy_page(info: WikiInfo, object_type: Optional[str]) -> bool:
    text = f"{info.title} {info.extract}".lower()
    if "may refer to" in text or "disambiguation" in text:
        return False
    bad_keywords = (
        "railroad",
        "railway",
        "locomotive",
        "diesel",
        "steam",
        "streamliner",
        "train",
        "pullman",
        "passenger",
        "station",
        "aircraft",
        "ship",
        "submarine",
        "destroyer",
        "highway",
        "road",
        "bridge",
        "building",
        "company",
        "corporation",
        "album",
        "song",
        "band",
        "film",
        "television",
        "episode",
        "novel",
        "comic",
        "manga",
        "school",
        "university",
    )
    if any(keyword in text for keyword in bad_keywords):
        return False
    good_keywords = (
        "galaxy",
        "nebula",
        "cluster",
        "open cluster",
        "globular cluster",
        "planetary nebula",
        "supernova",
        "supernova remnant",
        "emission nebula",
        "reflection nebula",
        "dark nebula",
        "h ii region",
        "hii",
        "constellation",
        "star",
        "astronomical",
        "deep-sky",
        "interstellar",
        "asterism",
    )
    if any(keyword in text for keyword in good_keywords):
        return True
    if object_type:
        for token in re.split(r"\\W+", object_type.lower()):
            if token and token in text:
                return True
    return False


def _apply_non_astronomy_reset(meta: Dict, openngc_entry: Optional[OpenNGCRecord]) -> None:
    fallback = openngc_entry.description if openngc_entry and openngc_entry.description else ""
    meta["description"] = fallback
    meta["external_link"] = ""
    meta["wiki_thumbnail"] = ""


def main() -> None:
    if not IC_META_PATH.exists():
        raise SystemExit(f"Missing metadata: {IC_META_PATH}")
    data = json.loads(IC_META_PATH.read_text(encoding="utf-8"))
    entries = data.get("IC", {})
    if not isinstance(entries, dict):
        raise SystemExit("IC metadata is not a dictionary.")

    openngc = _load_openngc()
    wiki = _build_wiki_index(entries.keys())

    discoverer_ids = sorted({rec.discoverer_id for rec in wiki.values() if rec.discoverer_id})
    discoverer_labels = _fetch_labels(discoverer_ids)

    updated = 0
    for object_id, meta in entries.items():
        if not isinstance(meta, dict):
            continue
        openngc_entry = openngc.get(object_id)
        wiki_entry = wiki.get(object_id)

        if not meta.get("name") and openngc_entry and openngc_entry.common_name:
            meta["name"] = openngc_entry.common_name
            updated += 1

        if meta.get("discoverer") is None and wiki_entry and wiki_entry.discoverer_id:
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

        if not meta.get("ra_hours") and openngc_entry and openngc_entry.ra_hours is not None:
            meta["ra_hours"] = openngc_entry.ra_hours
            updated += 1

        if not meta.get("dec_deg") and openngc_entry and openngc_entry.dec_deg is not None:
            meta["dec_deg"] = openngc_entry.dec_deg
            updated += 1

        if not meta.get("description") and openngc_entry and openngc_entry.description:
            meta["description"] = openngc_entry.description
            updated += 1

        if not meta.get("best_months"):
            best_months = _best_months_from_ra(
                openngc_entry.ra_hours if openngc_entry else meta.get("ra_hours")
            )
            if best_months:
                meta["best_months"] = best_months
                updated += 1

    print(f"Updated fields from OpenNGC/Wikidata: {updated}")
    print(f"Wikidata matches: {len(wiki)}")

    titles = {}
    for object_id in entries:
        title = _title_from_object_id(object_id)
        if title:
            titles[object_id] = title

    print(f"Fetching Wikipedia summaries for {len(titles)} IC entries...")
    wiki_info = _fetch_wiki_info(sorted(set(titles.values())))
    updated_wiki = 0
    for object_id, title in titles.items():
        info = wiki_info.get(title)
        if not info:
            continue
        meta = entries.get(object_id)
        if not isinstance(meta, dict):
            continue
        if not _looks_astronomy_page(info, meta.get("type")):
            _apply_non_astronomy_reset(meta, openngc.get(object_id))
            updated_wiki += 1
            continue
        meta["description"] = info.extract
        if info.fullurl:
            meta["external_link"] = info.fullurl
        if info.thumbnail:
            meta["wiki_thumbnail"] = info.thumbnail
        updated_wiki += 1

    missing_name_titles: Dict[str, str] = {}
    for object_id, meta in entries.items():
        if meta.get("description"):
            continue
        name = (meta.get("name") or "").strip()
        if not name or name.lower() == object_id.lower():
            continue
        missing_name_titles[object_id] = name

    if missing_name_titles:
        print(f"Fetching Wikipedia summaries for {len(missing_name_titles)} IC common names...")
        name_wiki = _fetch_wiki_info(sorted(set(missing_name_titles.values())))
        for object_id, name in missing_name_titles.items():
            info = name_wiki.get(name)
            if not info:
                continue
            meta = entries.get(object_id)
            if not isinstance(meta, dict):
                continue
            if not _looks_astronomy_page(info, meta.get("type")):
                _apply_non_astronomy_reset(meta, openngc.get(object_id))
                updated_wiki += 1
                continue
            meta["description"] = info.extract
            if info.fullurl:
                meta["external_link"] = info.fullurl
            if info.thumbnail:
                meta["wiki_thumbnail"] = info.thumbnail
            updated_wiki += 1

    IC_META_PATH.write_text(json.dumps({"IC": entries}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"IC entries updated with Wikipedia data: {updated_wiki}")


if __name__ == "__main__":
    main()
