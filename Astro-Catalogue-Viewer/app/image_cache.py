from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import time
from typing import Dict, Optional

from PySide6 import QtCore, QtGui


@dataclass
class CacheEntry:
    pixmap: QtGui.QPixmap
    last_access: float


class ThumbnailCache:
    def __init__(self, cache_dir: Path, thumb_size: int, memory_items: int = 512) -> None:
        self.cache_dir = cache_dir
        self.thumb_size = thumb_size
        self.memory_items = memory_items
        self._memory: Dict[str, CacheEntry] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, image_path: Path) -> str:
        stat = image_path.stat()
        payload = f"{image_path.resolve()}:{stat.st_mtime_ns}:{self.thumb_size}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.png"

    def _prune(self) -> None:
        if len(self._memory) <= self.memory_items:
            return
        sorted_items = sorted(self._memory.items(), key=lambda kv: kv[1].last_access)
        for key, _ in sorted_items[: len(self._memory) - self.memory_items]:
            self._memory.pop(key, None)

    def get_thumbnail(self, image_path: Path) -> Optional[QtGui.QPixmap]:
        key = self._cache_key(image_path)
        entry = self._memory.get(key)
        if entry:
            entry.last_access = time.time()
            return entry.pixmap

        disk_path = self._cache_path(key)
        if disk_path.exists():
            pixmap = QtGui.QPixmap(str(disk_path))
            if not pixmap.isNull():
                self._memory[key] = CacheEntry(pixmap=pixmap, last_access=time.time())
                self._prune()
                return pixmap
        return None

    def store_thumbnail(self, image_path: Path, pixmap: QtGui.QPixmap) -> None:
        key = self._cache_key(image_path)
        self._memory[key] = CacheEntry(pixmap=pixmap, last_access=time.time())
        self._prune()
        disk_path = self._cache_path(key)
        pixmap.save(str(disk_path), "PNG")

    def store_thumbnail_image(self, image_path: Path, image: QtGui.QImage) -> QtGui.QPixmap:
        key = self._cache_key(image_path)
        pixmap = QtGui.QPixmap.fromImage(image)
        self._memory[key] = CacheEntry(pixmap=pixmap, last_access=time.time())
        self._prune()
        disk_path = self._cache_path(key)
        image.save(str(disk_path), "PNG")
        return pixmap

    def create_thumbnail(self, image_path: Path) -> Optional[QtGui.QImage]:
        reader = QtGui.QImageReader(str(image_path))
        if not reader.canRead():
            return None
        reader.setAutoTransform(True)
        reader.setScaledSize(QtCore.QSize(self.thumb_size, self.thumb_size))
        image = reader.read()
        if image.isNull():
            return None
        return image
