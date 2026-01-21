import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def configure_logger(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def normalize_team(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace()).strip()


def normalize_team_name(name: str) -> str:
    if not name:
        return ""
    raw = name.replace("ø", "o").replace("Ø", "O").replace("å", "a").replace("Å", "A").replace("æ", "ae").replace("Æ", "Ae").replace("œ", "oe").replace("Œ", "Oe")
    raw = unicodedata.normalize("NFKD", raw)
    raw = raw.encode("ascii", "ignore").decode("ascii")
    raw = re.sub(r"[^a-zA-Z0-9\\s]", " ", raw)
    raw = raw.lower()
    tokens = raw.split()
    stop = {
        "fc", "sc", "afc", "cf", "ac", "club", "the", "de", "la", "el", "cd", "ud", "sv", "fk", "bk",
    }
    filtered = [t for t in tokens if t not in stop]
    return " ".join(filtered).strip()


def safe_json_dump(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)


def safe_json_load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class HttpClient:
    session: requests.Session
    logger: logging.Logger
    calls: int = field(default=0, init=False)

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None, timeout: int = 12) -> requests.Response:
        backoff = 0.8
        for attempt in range(4):
            try:
                self.calls += 1
                response = self.session.get(url, params=params, headers=headers, timeout=timeout)
                if response.status_code in (429, 500, 502, 503, 504):
                    self.logger.warning("HTTP %s %s -> %s", url, params, response.status_code)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                return response
            except requests.RequestException as exc:
                self.logger.warning("HTTP error %s: %s", url, exc)
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError(f"HTTP request failed after retries: {url}")


def build_http_client(logger: logging.Logger) -> HttpClient:
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "tipss_bot/2.0"})
    return HttpClient(session=session, logger=logger)
