import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)


@dataclass
class ApiConfig:
    base_url: str
    api_key: str
    timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_factor: float = 0.5


class SoccerApiClient:
    def __init__(self, config: ApiConfig) -> None:
        self._config = config
        self._session = requests.Session()
        retries = Retry(
            total=config.max_retries,
            backoff_factor=config.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST", "PUT", "DELETE"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.api_key}"}

    def get(self, path: str) -> dict[str, Any]:
        url = urljoin(self._config.base_url.rstrip("/") + "/", path.lstrip("/"))
        logger.info("Requesting %s", url)
        response = self._session.get(url, headers=self._headers(), timeout=self._config.timeout_seconds)
        if response.status_code >= 400:
            logger.error("API error status=%s body=%s", response.status_code, response.text)
            response.raise_for_status()
        return response.json()