"""Bounded client for Silo's native v2 API."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class UpstreamError(Exception):
    """Safe to expose: never contains credentials, response bodies, or URLs."""

    def __init__(self, message, status=None):
        self.status = status
        super().__init__(message)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SiloClient:
    def __init__(self, config):
        self.config = config

    def get(self, path, params=None, *, public=False, timeout=5):
        return self.request(path, params, public=public, timeout=timeout)

    def post(self, path, body):
        return self.request(path, body=body, timeout=8)

    def request(self, path, params=None, *, public=False, timeout=5, body=None):
        url = self.config.silo_url.rstrip("/") + "/api/v2" + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        headers = {"Accept": "application/json", "User-Agent": "silo-tracearr-bridge/0.1"}
        if not public:
            headers["Authorization"] = "Bearer " + self.config.silo_api_key
        encoded = None
        if body is not None:
            encoded = json.dumps(body, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        try:
            # No redirects: never forward the Silo key to another origin.
            with build_opener(NoRedirects).open(Request(url, data=encoded, headers=headers), timeout=timeout) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise UpstreamError("Silo response exceeded 8 MiB")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise UpstreamError("Silo returned an unexpected JSON shape")
                return data
        except HTTPError as error:
            code = error.code
            error.close()
            raise UpstreamError(f"Silo returned HTTP {code} for {path}", status=code) from None
        except (URLError, TimeoutError, OSError):
            raise UpstreamError(f"Silo connection failed for {path}") from None
        except (ValueError, UnicodeError):
            raise UpstreamError(f"Silo returned invalid JSON for {path}") from None

    def collection(self, path, params=None, *, budget=8):
        params = dict(params or {})
        params["limit"] = 200
        items, seen = [], set()
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamError(f"Silo pagination timed out for {path}")
            data = self.get(path, params, timeout=min(5, remaining))
            rows = data.get("items")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise UpstreamError(f"Silo returned an invalid collection for {path}")
            items.extend(rows)
            if len(items) > 200_000:
                raise UpstreamError("Silo collection exceeded 200,000 rows")
            page = data.get("page", {})
            if not isinstance(page, dict):
                raise UpstreamError("Silo returned invalid pagination")
            cursor = page.get("next_cursor")
            if not cursor:
                if page.get("has_more"):
                    raise UpstreamError("Silo pagination is missing its next cursor")
                return items
            if not isinstance(cursor, str) or cursor in seen or not rows:
                raise UpstreamError("Silo pagination did not advance")
            seen.add(cursor)
            params["cursor"] = cursor
