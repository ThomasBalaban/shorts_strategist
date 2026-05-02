from typing import Any, Dict, List, Optional

import requests

import config


class AnalyzerClient:
    def __init__(self, base_url: Optional[str] = None,
                 health_timeout: float = 2.0, read_timeout: float = 10.0):
        self.base_url = (base_url or config.ANALYZER_URL).rstrip("/")
        self.health_timeout = health_timeout
        self.read_timeout = read_timeout

    def health(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/health", timeout=self.health_timeout)
            return r.ok
        except requests.RequestException:
            return False

    def list_results(self) -> List[Dict[str, Any]]:
        try:
            r = requests.get(f"{self.base_url}/results", timeout=self.read_timeout)
            r.raise_for_status()
            return r.json() or []
        except requests.RequestException:
            return []

    def read_result(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(f"{self.base_url}/results/read",
                             params={"name": name}, timeout=self.read_timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return None

    def get_videos(self, output: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(f"{self.base_url}/videos",
                             params={"output": output}, timeout=self.read_timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return None
