"""A small Jellyfin client, standard library only. Add the calls your app needs here.

Authentication uses an API key (Jellyfin: Dashboard > API Keys) in the Authorization header.
Every failure becomes a JellyfinError with a short message, so callers print it instead of crashing.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast


class JellyfinError(Exception):
    """A request failed. The message names the path and the reason."""


class Jellyfin:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    page_size = 1000  # items asked for per page by paged()

    def _get(self, path: str, params: dict[str, Any] | None = None, auth: bool = True) -> Any:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(self.base + path)  # noqa: S310 - config.py only accepts http(s) URLs
        if auth:
            req.add_header("Authorization", f'MediaBrowser Token="{self.key}"')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - config.py only accepts http(s) URLs
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise JellyfinError(f"{path}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise JellyfinError(f"{path}: {e}") from e

    def public_info(self) -> dict[str, Any]:
        """Server name and version. Needs no key, so it tells 'unreachable' apart from 'bad key'."""
        return cast(dict[str, Any], self._get("/System/Info/Public", auth=False))

    def info(self) -> dict[str, Any]:
        """Needs a valid key."""
        return cast(dict[str, Any], self._get("/System/Info"))

    def libraries(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._get("/Library/VirtualFolders"))

    def users(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._get("/Users"))

    def sessions(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._get("/Sessions"))

    def paged(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Fetch every item of a list call, one page at a time, until TotalRecordCount is reached."""
        items, start = [], 0
        while True:
            page = self._get(path, {**params, "StartIndex": start, "Limit": self.page_size}) or {}
            got = page.get("Items", [])
            items += got
            start += len(got)
            if not got or start >= page.get("TotalRecordCount", 0):
                return items
