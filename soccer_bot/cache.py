import os
from dataclasses import dataclass
from typing import Any

from soccer_bot.utils import safe_json_dump, safe_json_load


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0


class DiskCache:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.stats = CacheStats()

    def _path(self, date_str: str, name: str) -> str:
        folder = os.path.join(self.base_dir, date_str)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{name}.json")

    def get(self, date_str: str, name: str) -> Any | None:
        path = self._path(date_str, name)
        if os.path.exists(path):
            self.stats.hits += 1
            return safe_json_load(path)
        self.stats.misses += 1
        return None

    def set(self, date_str: str, name: str, payload: Any) -> str:
        path = self._path(date_str, name)
        safe_json_dump(path, payload)
        return path

    def assert_single_dir(self, date_str: str) -> None:
        if not os.path.isdir(self.base_dir):
            return
        matches = [name for name in os.listdir(self.base_dir) if name == date_str]
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate cache dir detected for {date_str}")
