from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import sys
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote
import re

PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))

DEFAULT_CONFIG = {
    "catalogs": [
        {
            "name": "Messier",
            "metadata_file": "data/object_metadata.json",
            "image_dirs": [],
        },
        {
            "name": "NGC",
            "metadata_file": "data/ngc_metadata.json",
            "image_dirs": [],
        },
        {
            "name": "IC",
            "metadata_file": "data/ic_metadata.json",
            "image_dirs": [],
        },
        {
            "name": "Caldwell",
            "metadata_file": "data/caldwell_metadata.json",
            "image_dirs": [],
        },
    ],
    "image_extensions": [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"],
    "thumb_size": 240,
    "observer": {"latitude": 0.0, "longitude": 0.0, "elevation_m": 0.0},
    "show_welcome": True,
    "master_image_dir": "",
    "archive_image_dir": "",
    "use_wiki_thumbnails": False,
    "auto_check_updates": True,
}


@dataclass(frozen=True)
class CatalogItem:
    object_id: str
    catalog: str
    name: str
    object_type: str
    distance_ly: Optional[float]
    discoverer: Optional[str]
    discovery_year: Optional[int]
    best_months: Optional[str]
    description: Optional[str]
    notes: Optional[str]
    image_notes: Dict[str, str]
    external_link: Optional[str]
    ra_hours: Optional[float]
    dec_deg: Optional[float]
    image_paths: List[Path]
    thumbnail_path: Optional[Path]

    @property
    def display_name(self) -> str:
        if self.name:
            return f"{self.object_id} - {self.name}"
        return self.object_id

    @property
    def unique_key(self) -> str:
        return f"{self.catalog}:{self.object_id}"


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def load_config(config_path: Path) -> Dict:
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return _merge_default_config(loaded)
    return _merge_default_config({})


def resolve_metadata_path(config: Dict, catalog_name: str) -> Optional[Path]:
    for catalog_cfg in config.get("catalogs", []):
        if catalog_cfg.get("name") == catalog_name:
            metadata_value = catalog_cfg.get("metadata_file")
            if not metadata_value:
                return None
            return _resolve_path(metadata_value)
    return None


def save_config(config_path: Path, config: Dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def _build_image_index(image_dirs: Iterable[Path], extensions: Iterable[str]) -> Dict[str, List[Path]]:
    exts = {ext.lower() for ext in extensions}
    index: Dict[str, List[Path]] = {}
    seen: Dict[str, set] = {}
    for image_dir in image_dirs:
        if not image_dir.exists():
            continue
        for root, _, files in os.walk(image_dir):
            for filename in files:
                suffix = Path(filename).suffix.lower()
                if suffix not in exts:
                    continue
                stem = Path(filename).stem.upper()
                matches = _extract_object_ids(stem)
                if not matches:
                    continue
                image_path = Path(root) / filename
                resolved = str(image_path.resolve())
                for object_id in matches:
                    seen.setdefault(object_id, set())
                    if resolved in seen[object_id]:
                        continue
                    seen[object_id].add(resolved)
                    index.setdefault(object_id, []).append(image_path)
    for object_id, paths in index.items():
        index[object_id] = sorted(paths, key=lambda p: p.name.lower())
    return index


def _load_catalog_metadata(metadata_path: Path) -> Dict[str, Dict]:
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except UnicodeDecodeError:
        with metadata_path.open("r", encoding="latin-1") as handle:
            return json.load(handle)


def load_catalog_items(config: Dict) -> List[CatalogItem]:
    items: List[CatalogItem] = []
    extensions = config.get("image_extensions", DEFAULT_CONFIG["image_extensions"])
    observer = config.get("observer", {})
    latitude = observer.get("latitude")
    longitude = observer.get("longitude") or 0.0
    master_dir = config.get("master_image_dir") or ""
    master_path = _resolve_path(master_dir) if master_dir else None

    for catalog_cfg in config.get("catalogs", []):
        catalog_name = catalog_cfg.get("name", "Unknown")
        catalog_prefix = _catalog_prefix(catalog_name)
        metadata_path = _resolve_path(catalog_cfg.get("metadata_file", ""))
        image_dirs = [_resolve_path(path) for path in catalog_cfg.get("image_dirs", [])]
        if master_path:
            image_dirs.append(master_path)
        image_index = _build_image_index(image_dirs, extensions)

        if not metadata_path.exists():
            continue

        catalog_entries: Dict[str, Dict] = {}
        if metadata_path.exists():
            catalog_data = _load_catalog_metadata(metadata_path)
            catalog_entries = _select_catalog_entries(catalog_data, catalog_name)
        for object_id, meta in catalog_entries.items():
            image_paths = image_index.get(object_id.upper(), [])
            thumbnail_path = _select_thumbnail(image_paths, meta.get("thumbnail"))
            ra_hours = _parse_ra(meta.get("ra_hours") or meta.get("ra"))
            dec_deg = _parse_dec(meta.get("dec_deg") or meta.get("dec"))
            best_months = _adjust_best_months(meta.get("best_months"), latitude)
            if not best_months and ra_hours is not None and dec_deg is not None and latitude is not None:
                best_months = _compute_best_months(ra_hours, dec_deg, latitude, longitude)
            items.append(
                CatalogItem(
                    object_id=object_id,
                    catalog=catalog_name,
                    name=_normalize_text(meta.get("name", "")),
                    object_type=_normalize_text(meta.get("type", "")),
                    distance_ly=meta.get("distance_ly"),
                    discoverer=_normalize_text(meta.get("discoverer")),
                    discovery_year=meta.get("discovery_year"),
                    best_months=best_months,
                    description=_normalize_text(meta.get("description")),
                    notes=_normalize_text(meta.get("notes")),
                    image_notes=_normalize_image_notes(meta.get("image_notes")),
                    external_link=_normalize_text(
                        meta.get("external_link")
                    ) or _default_external_link(object_id, meta.get("name")),
                    ra_hours=ra_hours,
                    dec_deg=dec_deg,
                    image_paths=image_paths,
                    thumbnail_path=thumbnail_path,
                )
            )

        # Add image-only entries that are not in metadata.
        for object_id, image_paths in image_index.items():
            if catalog_prefix and not object_id.upper().startswith(catalog_prefix):
                continue
            if object_id in catalog_entries:
                continue
            thumbnail_path = image_paths[0] if image_paths else None
            items.append(
                CatalogItem(
                    object_id=object_id,
                    catalog=catalog_name,
                    name="",
                    object_type="",
                    distance_ly=None,
                    discoverer=None,
                    discovery_year=None,
                    best_months=None,
                    description=None,
                    notes=None,
                    image_notes={},
                    external_link=_default_external_link(object_id, None),
                    ra_hours=None,
                    dec_deg=None,
                    image_paths=image_paths,
                    thumbnail_path=thumbnail_path,
                )
            )

    return items


def _select_thumbnail(image_paths: List[Path], thumbnail_value: Optional[str]) -> Optional[Path]:
    if not image_paths:
        return None
    if not thumbnail_value:
        return image_paths[0]
    normalized = thumbnail_value.strip()
    for path in image_paths:
        if path.name == normalized:
            return path
    for path in image_paths:
        if path.stem == normalized:
            return path
    return image_paths[0]


def _select_catalog_entries(catalog_data: Dict[str, Dict], catalog_name: str) -> Dict[str, Dict]:
    if not isinstance(catalog_data, dict):
        return {}
    entries = catalog_data.get(catalog_name)
    if isinstance(entries, dict):
        return entries
    lower_name = (catalog_name or "").lower()
    for key, value in catalog_data.items():
        if isinstance(key, str) and key.lower() == lower_name and isinstance(value, dict):
            return value
    if len(catalog_data) == 1:
        only_value = next(iter(catalog_data.values()))
        if isinstance(only_value, dict):
            return only_value
    return {}


def collect_object_types(items: Iterable[CatalogItem]) -> List[str]:
    types = sorted({item.object_type for item in items if item.object_type})
    return types


def save_note(metadata_path: Path, catalog_name: str, object_id: str, notes: str) -> None:
    if not metadata_path.exists():
        return
    data = _load_catalog_metadata(metadata_path)
    catalog = data.setdefault(catalog_name, {})
    entry = catalog.setdefault(object_id, {})
    if notes.strip():
        entry["notes"] = notes
    else:
        entry.pop("notes", None)
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def save_image_note(
    metadata_path: Path,
    catalog_name: str,
    object_id: str,
    image_name: str,
    notes: str,
) -> None:
    if not metadata_path.exists():
        return
    data = _load_catalog_metadata(metadata_path)
    catalog = data.setdefault(catalog_name, {})
    entry = catalog.setdefault(object_id, {})
    image_notes = entry.setdefault("image_notes", {})
    if not isinstance(image_notes, dict):
        image_notes = {}
        entry["image_notes"] = image_notes
    if notes.strip():
        image_notes[image_name] = notes
    else:
        image_notes.pop(image_name, None)
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def save_thumbnail(metadata_path: Path, catalog_name: str, object_id: str, thumbnail_name: str) -> None:
    if not metadata_path.exists():
        return
    data = _load_catalog_metadata(metadata_path)
    catalog = data.setdefault(catalog_name, {})
    entry = catalog.setdefault(object_id, {})
    entry["thumbnail"] = thumbnail_name
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def _merge_default_config(loaded: Dict) -> Dict:
    merged = DEFAULT_CONFIG.copy()
    merged.update(loaded)
    merged.setdefault("observer", DEFAULT_CONFIG["observer"])
    merged.setdefault("image_extensions", DEFAULT_CONFIG["image_extensions"])
    merged.setdefault("thumb_size", DEFAULT_CONFIG["thumb_size"])

    existing_catalogs = {c.get("name"): c for c in loaded.get("catalogs", []) if isinstance(c, dict)}
    catalogs = []
    for default_catalog in DEFAULT_CONFIG["catalogs"]:
        name = default_catalog.get("name")
        if name in existing_catalogs:
            updated = default_catalog.copy()
            updated.update(existing_catalogs[name])
            catalogs.append(updated)
        else:
            catalogs.append(default_catalog.copy())
    # include any custom catalogs not in defaults
    for name, catalog in existing_catalogs.items():
        if name not in {c.get("name") for c in catalogs}:
            catalogs.append(catalog)
    merged["catalogs"] = catalogs
    _normalize_catalog_paths(merged)
    return merged


def _normalize_catalog_paths(config: Dict) -> None:
    default_map = {c.get("name"): c for c in DEFAULT_CONFIG.get("catalogs", [])}
    for catalog in config.get("catalogs", []):
        name = catalog.get("name")
        default_catalog = default_map.get(name, {})
        image_dirs = [path for path in catalog.get("image_dirs", []) if path]
        existing = [path for path in image_dirs if _resolve_path(path).exists()]
        if existing:
            catalog["image_dirs"] = existing
        else:
            default_dirs = default_catalog.get("image_dirs", [])
            if default_dirs:
                catalog["image_dirs"] = list(default_dirs)
            elif image_dirs:
                catalog["image_dirs"] = image_dirs
    master_dir = config.get("master_image_dir") or ""
    if master_dir and not _resolve_path(master_dir).exists():
        config["master_image_dir"] = ""


def _extract_object_ids(stem: str) -> List[str]:
    ids = []
    for match in re.findall(r"\b(M|NGC|IC|C)\s*0*(\d{1,5})\b", stem):
        prefix, number = match
        ids.append(f"{prefix}{int(number)}")
    return ids


def _normalize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return value.replace("M\u008echain", "M\u00e9chain")


def _normalize_image_notes(value: Optional[Dict]) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, str] = {}
    for key, note in value.items():
        if not isinstance(key, str):
            continue
        if not isinstance(note, str):
            continue
        normalized[key] = _normalize_text(note) or ""
    return normalized


def _default_external_link(object_id: str, name: Optional[str]) -> str:
    match = re.match(r"^M\\s*0*(\\d+)$", object_id, re.IGNORECASE)
    if match:
        messier_num = int(match.group(1))
        return f"https://en.wikipedia.org/wiki/Messier_{messier_num}"
    target = name or object_id
    slug = quote(target.replace(" ", "_"))
    return f"https://en.wikipedia.org/wiki/{slug}"


def _catalog_prefix(catalog_name: str) -> str:
    name = (catalog_name or "").strip().lower()
    if name == "messier":
        return "M"
    if name == "ngc":
        return "NGC"
    if name == "ic":
        return "IC"
    if name == "caldwell":
        return "C"
    return ""


def _adjust_best_months(best_months: Optional[str], latitude: Optional[float]) -> Optional[str]:
    if not best_months:
        return best_months
    if latitude is None or latitude >= 0:
        return best_months
    month_map = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    months = []
    for idx in range(0, len(best_months), 3):
        chunk = best_months[idx: idx + 3]
        if chunk in month_map:
            months.append(chunk)
    if not months:
        return best_months
    shifted = []
    for month in months:
        new_index = (month_map.index(month) + 6) % 12
        shifted.append(month_map[new_index])
    return "".join(shifted)


def _parse_ra(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    parts = re.split(r"[:\s]+", text)
    try:
        hours = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
        return hours + minutes / 60.0 + seconds / 3600.0
    except ValueError:
        return None


def _parse_dec(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    parts = re.split(r"[:\s]+", text)
    try:
        deg = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
        return sign * (deg + minutes / 60.0 + seconds / 3600.0)
    except ValueError:
        return None


def _compute_best_months(ra_hours: float, dec_deg: float, lat_deg: float, lon_deg: float) -> str:
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    best = []
    for month in range(1, 13):
        date = datetime(2025, month, 15, 0, 0, tzinfo=timezone.utc)
        lst = _local_sidereal_time(date, lon_deg)
        ha = (lst - ra_hours) * 15.0
        ha = (ha + 180.0) % 360.0 - 180.0
        alt = _altitude_deg(lat_deg, dec_deg, ha)
        if alt >= 25.0:
            best.append(month_names[month - 1])
    return "".join(best)


def _local_sidereal_time(date: datetime, longitude_deg: float) -> float:
    jd = _julian_date(date)
    t = (jd - 2451545.0) / 36525.0
    gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t * t * t / 38710000.0
    gmst = gmst % 360.0
    lst = (gmst + longitude_deg) % 360.0
    return lst / 15.0


def _julian_date(date: datetime) -> float:
    year = date.year
    month = date.month
    day = date.day + (date.hour + date.minute / 60.0) / 24.0
    if month <= 2:
        year -= 1
        month += 12
    a = math.floor(year / 100)
    b = 2 - a + math.floor(a / 4)
    jd = math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5
    return jd


def _altitude_deg(lat_deg: float, dec_deg: float, ha_deg: float) -> float:
    lat_rad = math.radians(lat_deg)
    dec_rad = math.radians(dec_deg)
    ha_rad = math.radians(ha_deg)
    sin_alt = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
    return math.degrees(math.asin(sin_alt))
