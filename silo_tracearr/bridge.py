"""The deliberately small Jellyfin API surface needed for Tracearr polling."""

import threading
import time
import re
from datetime import datetime
from collections import OrderedDict

from . import __version__, mapping
from .client import SiloClient, UpstreamError
from .artwork import Artwork


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
        self.artwork = Artwork(config)
        self.control_cache = Cache()
        self.reason_lock = threading.Lock()
        self.reasons = OrderedDict()

    def can_terminate(self):
        def load():
            try:
                caps = self.client.get("/admin/sessions/command-capabilities", timeout=2)
                return caps.get("terminate_revokes_authority") is True and "terminate" in caps.get("actions", [])
            except UpstreamError:
                # Older servers or unavailable controls must not break monitoring.
                return False
        return self.control_cache.get("terminate", 30, load)

    def post(self, path, body):
        match = re.fullmatch(r"/Sessions/([A-Za-z0-9_-]{1,128})/(Message|Playing/Stop)", path, re.IGNORECASE)
        if not match:
            raise APIError(405, "Only session termination and its preceding reason are supported")
        if not self.can_terminate():
            raise APIError(501, "Silo does not advertise durable session termination")
        session_id, action = match.groups()
        if action.lower() == "message":
            # Tracearr sends Message then Playing/Stop. Stage the reason so
            # Silo can deliver it with its own termination notification.
            text = body.get("Text")
            if not isinstance(text, str) or not text or len(text) > 1024:
                raise APIError(400, "Termination reason must contain 1–1024 characters")
            with self.reason_lock:
                now = time.monotonic()
                self.reasons = OrderedDict((k, v) for k, v in self.reasons.items() if now < v[0])
                self.reasons[session_id] = (now + 60, text)
                self.reasons.move_to_end(session_id)
                while len(self.reasons) > 256:
                    self.reasons.popitem(last=False)
            return {"queued": True}
        if body:
            raise APIError(400, "Playing/Stop does not accept a request body")
        with self.reason_lock:
            pending = self.reasons.get(session_id)
            reason = pending[1] if pending and time.monotonic() < pending[0] else "Stream terminated by Tracearr"
        try:
            receipt = self.client.post(f"/admin/sessions/{session_id}/terminate", {"reason": reason})
        except UpstreamError as error:
            if error.status in (401, 403):
                raise APIError(403, "Silo denied termination: use an unscoped admin key or add admin:sessions:control") from None
            if error.status in (404, 409):
                raise APIError(error.status, "Silo could not terminate this session (missing or conflicting state)") from None
            raise
        if (receipt.get("session_id") != session_id or receipt.get("authority_revoked") is not True
                or receipt.get("durable_state") != "stopped"):
            raise UpstreamError("Silo did not confirm playback authority was revoked")
        with self.reason_lock:
            if self.reasons.get(session_id) == pending:
                self.reasons.pop(session_id, None)
        return receipt

    def sessions(self):
        controllable = self.can_terminate()
        result = []
        for row in self.client.collection("/admin/sessions"):
            mapped = mapping.session(row)
            # This advertises the bridge's server-side Stop implementation;
            # no player control connection is required for authority revocation.
            mapped["SupportsMediaControl"] = controllable
            mapped["SupportsRemoteControl"] = controllable
            item = mapped["NowPlayingItem"]
            tag = self.artwork.register(item["Id"], row.get("poster_url"))
            if tag:
                item["ImageTags"] = {"Primary": tag}
            result.append(mapped)
        return result

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
        image_route = re.fullmatch(r"/Items/([^/]+)/Images/Primary(?:/0)?", path, re.IGNORECASE)
        if image_route:
            item_id = image_route[1]
            image = self.artwork.get(item_id, query.get("tag"))
            if image is None:
                # A restart loses registrations; recover from current playback.
                self.sessions()
                image = self.artwork.get(item_id, query.get("tag"))
            if image is None:
                raise APIError(404, "No playback poster available for this item")
            return image
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
            return self.sessions()
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
