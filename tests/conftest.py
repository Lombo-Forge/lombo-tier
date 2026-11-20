"""A fake Jellyfin and a fake webhook on localhost, so tests need no real server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest


class Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.key = "good-key"
        self.libraries = [{"Name": "Movies", "Locations": ["/media/movies"]}, {"Name": "Empty", "Locations": []}]
        self.users: list[dict] = []
        self.sessions: list[dict] = []
        self.items: list[dict] = []
        self.user_items: dict = {}  # user id -> {"IsPlayed": [...], "IsResumable": [...]}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, payload=None):
                data = json.dumps(payload if payload is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _page(self, items, query):
                start = int(query.get("StartIndex", 0))
                limit = int(query.get("Limit", len(items) or 1))
                return {"Items": items[start : start + limit], "TotalRecordCount": len(items)}

            def do_GET(self):
                url = urlparse(self.path)
                path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
                outer.requests.append(
                    {"method": "GET", "path": self.path, "query": query, "auth": self.headers.get("Authorization")}
                )
                if path == "/System/Info/Public":
                    return self._reply(200, {"ServerName": "Fake", "Version": "0.0.0"})
                if self.headers.get("Authorization") != f'MediaBrowser Token="{outer.key}"':
                    return self._reply(401)
                if path == "/System/Info":
                    return self._reply(200, {"ServerName": "Fake"})
                if path == "/Library/VirtualFolders":
                    return self._reply(200, outer.libraries)
                if path == "/Users":
                    return self._reply(200, outer.users)
                if path == "/Sessions":
                    return self._reply(200, outer.sessions)
                if path == "/Items":
                    return self._reply(200, self._page(outer.items, query))
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "Users" and parts[2] == "Items":
                    lists = outer.user_items.get(parts[1], {})
                    return self._reply(200, self._page(lists.get(query.get("Filters"), []), query))
                return self._reply(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"method": "POST", "path": self.path, "body": json.loads(body)})
                self._reply(204)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.server.shutdown()


@pytest.fixture
def env(fake):
    return {
        "LOMBO_TIER_JELLYFIN_URL": fake.url,
        "LOMBO_TIER_JELLYFIN_API_KEY": "good-key",
        "LOMBO_TIER_WEBHOOK_URL": fake.url + "/hook",
    }
