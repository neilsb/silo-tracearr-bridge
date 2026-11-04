"""The deliberately small Jellyfin API surface needed for Tracearr polling."""

import threading
import time
from datetime import datetime

from . import __version__, mapping
from .client import SiloClient, UpstreamError


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}

    def get(self, key, ttl, load):
        with self.lock:
            saved = self.values.get(key)
            if saved and time.monotonic() < saved[0]:
                return saved[1]
            # Only publish complete snapshots. Failed fetches never become []:
            # Tracearr would interpret that as stopped sessions/deleted media.
            value = load()
            self.values[key] = (time.monotonic() + ttl, value)
            return value


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timestamp must include its timezone")
    return result


class Bridge:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or SiloClient(config)
        self.cache = Cache()

    def verify(self):
        caps = self.client.get("/admin/sessions/capabilities")
        if caps.get("available") is not True:
            raise UpstreamError("Silo live session monitoring is unavailable")
        self.client.get("/admin/users", {"limit": 1})
        self.client.get("/user/libraries")
        catalog = self.client.get("/admin/catalog/capabilities")
        if catalog.get("item_listing") is not True:
            raise UpstreamError("Silo catalog monitoring is unavailable")

    def info(self):
        identity = self.client.get("/system/identity", public=True)
        if not isinstance(identity.get("server_id"), str) or not identity["server_id"]:
            raise UpstreamError("Silo did not provide its stable server identity")
        return {"Id": identity["server_id"], "ServerName": self.config.name,
                "ProductName": "Silo Tracearr Bridge", "Version": __version__,
                "StartupWizardCompleted": True}

    def users(self):
        return self.cache.get("users", 30, lambda: [mapping.user(row) for row in self.client.collection("/admin/users")])

    def libraries(self):
        return self.cache.get("libraries", 60, lambda: [mapping.library(row) for row in self.client.collection("/user/libraries")
                                                       if row["type"] in ("movies", "series", "mixed")])

    def catalog(self):
        return self.cache.get("catalog", 60, lambda: self.client.collection("/admin/catalog/items", budget=25))

    def items(self, query):
        # Tracearr uses account-scoped Items only for historic watched state.
        # Do not pretend the current catalog is watch history.
        if "filters" in query or "userid" in query:
            raise APIError(501, "Watch-history import and user-scoped items are not implemented")
        allowed = {"ids", "parentid", "includeitemtypes", "startindex", "limit", "sortby", "sortorder",
                   "recursive", "fields", "ismissing", "collapseboxsetitems", "mindatelastsaved", "enabletotalrecordcount"}
        if query.keys() - allowed:
            raise APIError(400, "Unsupported item query parameter")
        try:
            start, limit = int(query.get("startindex", "0")), int(query.get("limit", "100"))
            if start < 0 or not 0 <= limit <= 10000:
                raise ValueError()
            ids = {mapping.native_id("item", x) for x in query["ids"].split(",")} if "ids" in query else None
            parent = mapping.native_id("library", query["parentid"]) if "parentid" in query else None
            since = timestamp(query["mindatelastsaved"]) if query.get("mindatelastsaved") else None
        except ValueError:
            raise APIError(400, "Invalid item IDs, timestamp or pagination") from None
        types = {x.lower() for x in query.get("includeitemtypes", "Movie,Series,Episode").split(",")}
        rows = [row for row in self.catalog()
                if row["type"] in types and (ids is None or row["content_id"] in ids)
                and (parent is None or parent in row["library_ids"])
                and (since is None or timestamp(row["added_at"]) >= since)]
        sort = query.get("sortby", "").lower()
        if sort not in ("", "datecreated", "sortname", "name"):
            raise APIError(400, "Unsupported sort field")
        key = (lambda row: timestamp(row["added_at"])) if sort == "datecreated" else (
            (lambda row: row["title"].casefold()) if sort else (lambda row: row["content_id"]))
        rows.sort(key=key, reverse=query.get("sortorder", "").lower() == "descending")
        return {"Items": [mapping.catalog_item(row) for row in rows[start:start + limit]],
                "TotalRecordCount": len(rows), "StartIndex": start}

    def get(self, path, query):
        if path.lower().startswith("/users/") and path.count("/") == 2 and path.lower() != "/users/me":
            match = next((u for u in self.users() if u["Id"] == path.split("/")[-1]), None)
            if match is None:
                raise APIError(404, "User not found")
            return match
        path = path.rstrip("/").lower()
        if path == "/health/live":
            return {"status": "ok", "version": __version__}
        if path == "/health/ready":
            self.verify()
            return {"status": "ready", "server": self.info()["Id"]}
        if path in ("/system/info", "/system/info/public"):
            return self.info()
        if path == "/users/me":
            # Jellyfin API keys have no user context. Tracearr then probes Auth/Keys.
            raise APIError(400, "API key has no user context")
        if path == "/auth/keys":
            self.verify()
            return {"Items": [], "TotalRecordCount": 0}
        if path == "/users":
            return self.users()
        if path == "/sessions":
            return [mapping.session(row) for row in self.client.collection("/admin/sessions")]
        if path == "/library/virtualfolders":
            return self.libraries()
        if path == "/items":
            return self.items(query)
        if path == "/plugins":
            return []
        if path == "/quickconnect/enabled":
            return False
        if path.startswith("/users/") or path == "/system/activitylog/entries":
            raise APIError(501, "History and member sign-in are not implemented")
        raise APIError(404, "Endpoint not implemented by this bridge")
