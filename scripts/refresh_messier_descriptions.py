#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "object_metadata.json"


def _fetch_json(url: str) -> Dict:
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
    if not payload:
        raise RuntimeError(f"Empty response for {url}")
    return json.loads(payload)


def _fetch_messier_table() -> Dict[str, Dict[str, str]]:
    params = {
        "action": "parse",
        "page": "Messier_object",
        "prop": "text",
        "format": "json",
    }
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    payload = _fetch_json(url)
    html = payload.get("parse", {}).get("text", {}).get("*", "")
    if not html:
        raise RuntimeError("Unable to fetch Messier table.")

    class TableParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_table = False
            self.in_row = False
            self.in_cell = False
            self.rows: List[List[str]] = []
            self.current_row: List[str] = []
            self.current_cell: List[str] = []
            self._table_found = False

        def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
            attrs_map = dict(attrs)
            if tag == "table" and "wikitable" in attrs_map.get("class", ""):
                if not self._table_found:
                    self.in_table = True
            if self.in_table and tag == "tr":
                self.in_row = True
                self.current_row = []
            if self.in_row and tag in ("th", "td"):
                self.in_cell = True
                self.current_cell = []

        def handle_endtag(self, tag: str) -> None:
            if self.in_cell and tag in ("th", "td"):
                self.in_cell = False
                cell = "".join(self.current_cell)
                cell = cell.replace("\xad", "")
                cell = re.sub(r"\s+", " ", cell).strip()
                self.current_row.append(cell)
            if self.in_row and tag == "tr":
                if self.current_row:
                    self.rows.append(self.current_row)
                self.in_row = False
            if self.in_table and tag == "table":
                if self.rows:
                    self.in_table = False
                    self._table_found = True

        def handle_data(self, data: str) -> None:
            if self.in_cell:
                self.current_cell.append(data)

    parser = TableParser()
    parser.feed(html)
    if not parser.rows:
        raise RuntimeError("Messier table parsing failed.")

    headers = [re.sub(r"\s+", " ", h).strip() for h in parser.rows[0]]
    index = {name: idx for idx, name in enumerate(headers)}
    table: Dict[str, Dict[str, str]] = {}

    for row in parser.rows[1:]:
        messier = row[index.get("Messier no.", 0)] if "Messier no." in index else row[0]
        messier = re.sub(r"\[.*?\]", "", messier).strip().replace(" ", "")
        if not messier.startswith("M"):
            continue
        entry: Dict[str, str] = {}
        for key, idx in index.items():
            if idx < len(row):
                entry[key] = re.sub(r"\[.*?\]", "", row[idx]).strip()
        table[messier] = entry

    return table


def _fetch_wiki_extract(title: str) -> str:
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "format": "json",
        "titles": title,
    }
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    payload = _fetch_json(url)
    pages = payload.get("query", {}).get("pages", {})
    if not pages:
        return ""
    page = next(iter(pages.values()))
    extract = page.get("extract") or ""
    extract = re.sub(r"\s+", " ", extract).strip()
    return extract


def _sentence_slice(text: str, target: int) -> str:
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    selected = []
    total = 0
    for sentence in parts:
        if not sentence:
            continue
        selected.append(sentence)
        total += len(sentence) + 1
        if total >= target:
            break
    return " ".join(selected).strip()


def _hemisphere_from_dec(dec_text: str) -> str:
    if not dec_text:
        return "equatorial"
    if dec_text.strip().startswith("-"):
        return "southern"
    if dec_text.strip().startswith("+"):
        return "northern"
    return "equatorial"


def _astro_notes(meta: Dict, table: Dict[str, str]) -> str:
    object_type = (meta.get("type") or table.get("Object type") or "").lower()
    constellation = table.get("Constellation", "").strip()
    dec_text = table.get("Declination", "")
    hemisphere = _hemisphere_from_dec(dec_text)
    mag = table.get("Apparent magnitude", "")
    size = table.get("Apparent dimensions", "")

    if hemisphere == "northern":
        sky = "northern"
    elif hemisphere == "southern":
        sky = "southern"
    else:
        sky = "equatorial"

    filter_note = ""
    if "planetary nebula" in object_type:
        filter_note = "OIII and Ha help pull out the shell and faint halos."
    elif "supernova" in object_type or "remnant" in object_type:
        filter_note = "Ha and OIII are the go-to filters for filamentary structure."
    elif "emission" in object_type or "h ii" in object_type:
        filter_note = "Ha, OIII, and SII reveal structure and make moonlit sessions viable."
    elif "reflection" in object_type:
        filter_note = "Broadband works best; narrowband adds little to reflection dust."
    elif "nebula" in object_type:
        filter_note = "Narrowband (Ha/OIII) helps, with broadband for star color."
    elif "galaxy" in object_type:
        filter_note = "Broadband LRGB is typical; Ha can highlight star-forming knots."
    elif "globular" in object_type:
        filter_note = "No narrowband needed; focus on star color and core resolution."
    elif "open cluster" in object_type or "cluster" in object_type:
        filter_note = "Broadband is sufficient; shorter subs keep bright stars tight."
    else:
        filter_note = "Broadband capture is a safe starting point."

    sky_line = f"Sky position: {sky} sky"
    if constellation:
        sky_line += f" in {constellation}"
    sky_line += "."

    brightness = []
    if mag:
        brightness.append(f"brightness around mag {mag}")
    if size:
        brightness.append(f"apparent size {size}")
    if brightness:
        brightness_line = "Target characteristics: " + ", ".join(brightness) + "."
    else:
        brightness_line = "Target characteristics: brightness and size vary; plan framing with a medium focal length."

    exposure_line = (
        "Capture tips: ISO 1600+ (or gain ~100-200), 120-300s subs, and 2-6 hours total integration. "
        "Faint structure benefits from longer stacks and careful calibration."
    )

    if "galaxy" in object_type or "nebula" in object_type or "remnant" in object_type:
        sky_quality = "Dark skies (Bortle 4 or better) or narrowband help tease out faint detail."
    else:
        sky_quality = "Moderate skies are workable, but darker sites improve contrast."

    detail_line = ""
    if "galaxy" in object_type:
        detail_line = "Look for spiral arms or dust lanes as seeing and integration improve."
    elif "globular" in object_type:
        detail_line = "Longer integration resolves more of the outer halo into stars."
    elif "open cluster" in object_type:
        detail_line = "Wide framing and gentle star control preserve color contrast."
    elif "nebula" in object_type or "remnant" in object_type:
        detail_line = "Longer stacks reveal filaments, shock fronts, and faint outer shells."
    else:
        detail_line = "Patience with integration time pays off in contrast and detail."

    return " ".join([sky_line, brightness_line, filter_note, sky_quality, exposure_line, detail_line]).strip()


def _build_description(
    object_id: str,
    meta: Dict,
    table: Dict[str, str],
    extract: str,
) -> str:
    name = meta.get("name") or object_id
    object_type = meta.get("type") or table.get("Object type") or "deep-sky object"
    constellation = table.get("Constellation", "")
    distance = table.get("Distance (kly)", "")
    mag = table.get("Apparent magnitude", "")
    size = table.get("Apparent dimensions", "")
    discoverer = meta.get("discoverer")
    discovery_year = meta.get("discovery_year")

    base_parts = [f"{object_id} ({name}) is a {object_type.lower()}"]
    if constellation:
        base_parts.append(f"in the constellation {constellation}")
    if distance:
        base_parts.append(f"about {distance} kly away")
    base_sentence = " ".join(base_parts) + "."

    stats = []
    if mag:
        stats.append(f"around magnitude {mag}")
    if size:
        stats.append(f"spanning roughly {size}")
    stats_sentence = ""
    if stats:
        stats_sentence = f"It appears {', '.join(stats)}."

    discover_sentence = ""
    if discoverer and discovery_year:
        discover_sentence = f"It was discovered by {discoverer} in {discovery_year}."
    elif discoverer:
        discover_sentence = f"It was discovered by {discoverer}."

    extract_sentence = _sentence_slice(extract, 420)
    if extract_sentence:
        extract_sentence = extract_sentence.rstrip(".") + "."

    notes = _astro_notes(meta, table)

    description = " ".join(
        part for part in [base_sentence, stats_sentence, discover_sentence, extract_sentence, "Astrophotography notes:", notes] if part
    ).strip()

    description = re.sub(r"\s+", " ", description).strip()
    description = _clamp_length(description, 800, 1000)
    return description


def _clamp_length(text: str, min_len: int, max_len: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        trimmed = []
        total = 0
        for sentence in sentences:
            if not sentence:
                continue
            if total + len(sentence) + 1 > max_len:
                break
            trimmed.append(sentence)
            total += len(sentence) + 1
        if trimmed:
            text = " ".join(trimmed).strip()
    if len(text) < min_len:
        filler = (
            " Use dithering and good calibration frames, and expect improved contrast with steady seeing."
        )
        while len(text) < min_len:
            text = (text + filler).strip()
        if len(text) > max_len:
            text = text[:max_len].rsplit(" ", 1)[0] + "."
    return text


def main() -> None:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    messier = data.get("Messier", {})
    table = _fetch_messier_table()
    updated = 0

    for object_id, meta in messier.items():
        match = re.match(r"^M\s*0*(\d+)$", object_id, re.IGNORECASE)
        if not match:
            continue
        num = int(match.group(1))
        title = f"Messier_{num}"
        extract = _fetch_wiki_extract(title)
        entry = table.get(f"M{num}")
        if not entry:
            entry = {}
        meta["external_link"] = f"https://en.wikipedia.org/wiki/Messier_{num}"
        meta["description"] = _build_description(object_id, meta, entry, extract)
        updated += 1
        time.sleep(0.1)

    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Updated {updated} Messier entries.")


if __name__ == "__main__":
    main()
