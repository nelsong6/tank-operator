import time
from threading import Lock

import httpx
import jwt


class GitHubAppTokenMinter:
    """Mints + caches GitHub App installation tokens.

    Installation tokens are valid for an hour. We refresh when <5 min left.
    """

    def __init__(self, app_id: str, installation_id: str, private_key: str) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._lock = Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def installation_token(self) -> str:
        with self._lock:
            if self._token and self._expires_at - time.time() > 300:
                return self._token
            self._token, self._expires_at = self._fetch()
            return self._token

    def _fetch(self) -> tuple[str, float]:
        now = int(time.time())
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self._app_id},
            self._private_key,
            algorithm="RS256",
        )
        r = httpx.post(
            f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        body = r.json()
        # expires_at is ISO8601; easier to just trust "~1h" and refresh 5min early.
        return body["token"], time.time() + 3300
