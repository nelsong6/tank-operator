from typing import Any

import httpx

from .auth import GitHubAppTokenMinter

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, minter: GitHubAppTokenMinter) -> None:
        self._minter = minter

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._minter.installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = httpx.get(f"{GITHUB_API}{path}", headers=self._headers(), params=params, timeout=15.0)
        r.raise_for_status()
        return r.json()
