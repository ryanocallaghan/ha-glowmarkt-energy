"""Minimal async client for the Glowmarkt (Bright) API."""

from __future__ import annotations

from datetime import UTC, datetime
import time
from typing import Any

import aiohttp

BASE_URL = "https://api.glowmarkt.com/api/v0-1"
# Public application ID of the Bright app, used by most community clients.
APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class GlowmarktError(Exception):
    """Error talking to the Glowmarkt API."""


class GlowmarktAuthError(GlowmarktError):
    """Credentials were rejected."""


class GlowmarktClient:
    """Talk to the Glowmarkt API with a Bright username and password."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_expires = 0.0

    async def authenticate(self) -> None:
        """Log in and store a fresh token."""
        try:
            async with self._session.post(
                f"{BASE_URL}/auth",
                json={"username": self._username, "password": self._password},
                headers={"applicationId": APPLICATION_ID},
                timeout=TIMEOUT,
            ) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GlowmarktError(f"Login request failed: {err}") from err

        if status in (401, 403) or not isinstance(data, dict) or not data.get("valid"):
            raise GlowmarktAuthError("Bright rejected the username or password")
        if "token" not in data:
            raise GlowmarktError(f"Unexpected login response (HTTP {status})")
        self._token = data["token"]
        # Tokens last about a week; fall back to 6 days if no expiry is given.
        self._token_expires = float(data.get("exp") or time.time() + 6 * 86400)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET a path, logging in first or again as needed."""
        if self._token is None or time.time() > self._token_expires - 3600:
            await self.authenticate()

        for attempt in range(2):
            try:
                async with self._session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers={"applicationId": APPLICATION_ID, "token": self._token},
                    timeout=TIMEOUT,
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        await self.authenticate()
                        continue
                    if resp.status == 401:
                        raise GlowmarktAuthError("Token rejected after re-login")
                    if resp.status >= 400:
                        raise GlowmarktError(f"GET {path} returned HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise GlowmarktError(f"GET {path} failed: {err}") from err
        raise GlowmarktError(f"GET {path} failed")

    async def resources(self) -> list[dict[str, Any]]:
        """Return the account's data streams (resources)."""
        data = await self._get("/resource")
        if not isinstance(data, list):
            raise GlowmarktError("Unexpected /resource response")
        return data

    async def catchup(self, resource_id: str) -> None:
        """Ask the DCC to pull the latest data from the meter (best effort)."""
        try:
            await self._get(f"/resource/{resource_id}/catchup")
        except GlowmarktAuthError:
            raise
        except GlowmarktError:
            pass

    async def first_reading(self, resource_id: str) -> datetime | None:
        """Return when the resource's data starts, if Bright says."""
        try:
            data = await self._get(f"/resource/{resource_id}/first-time")
        except GlowmarktAuthError:
            raise
        except GlowmarktError:
            return None
        info = data.get("data") if isinstance(data, dict) else None
        ts = info.get("firstTs") if isinstance(info, dict) else None
        return datetime.fromtimestamp(ts, UTC) if isinstance(ts, int | float) else None

    async def readings(
        self, resource_id: str, start: datetime, end: datetime, period: str = "PT1H"
    ) -> list[tuple[datetime, float]]:
        """Return (bucket start in UTC, value) pairs between start and end."""
        fmt = "%Y-%m-%dT%H:%M:%S"
        data = await self._get(
            f"/resource/{resource_id}/readings",
            {
                "from": start.astimezone(UTC).strftime(fmt),
                "to": end.astimezone(UTC).strftime(fmt),
                "period": period,
                "function": "sum",
                "offset": "0",
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise GlowmarktError("Unexpected readings response")
        return [
            (datetime.fromtimestamp(ts, UTC), float(value or 0))
            for ts, value in data["data"]
        ]
