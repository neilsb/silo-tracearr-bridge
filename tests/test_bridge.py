"""HTTP integration tests using an independent, paginated fake native Silo API."""

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from silo_tracearr.bridge import Bridge
from silo_tracearr.config import Config
from silo_tracearr.mapping import catalog_item, native_id, resource_id, session
from silo_tracearr.server import Server


SESSION = {
    "session_id": "native-play-42", "user_id": "7", "username": "alice",
    "profile_id": "family", "media_file_id": "22", "content_id": "episode-tvdb-88-0-2",
    "media_title": "Special episode", "episode_name": "Bonus", "media_type": "episode",
    "series_name": "Example series", "season_number": 0, "episode_number": 2,
    "file_duration": 1800, "position_seconds": 90.25, "is_paused": False,
    "play_method": "direct", "effective_play_method": "direct",
    "updated_at": "2026-11-04T12:00:00Z", "client_ip": "203.0.113.8",
    "client_name": "Silo Apple", "client_label": "Living Room TV",
    "device_id": "living-room-tv", "source_video_codec": "hevc",
    "source_video_height": 1600, "source_audio_codec": "aac", "source_audio_channels": 6,
    "source_bitrate_kbps": 12000, "source_container": "mkv", "audio_track_index": 1,
}
USERS = [
    {"id": "7", "username": "alice", "enabled": True, "role": "admin", "effective_policy": {"library_ids": None}},
    {"id": "8", "username": "bob", "enabled": False, "role": "user", "effective_policy": {"library_ids": ["2"]}},
]
CATALOG = [
    {"content_id": "movie-tmdb-10", "title": "Example movie", "type": "movie", "year": 2025,
     "tmdb_id": "10", "library_ids": ["1"], "added_at": "2026-11-01T00:00:00Z",
     "versions": [{"file_id": "21", "library_id": "1", "file_size": 1000000,
                   "codec_video": "h264", "codec_audio": "aac", "audio_channels": 2, "bitrate_kbps": 5000}]},
    {"content_id": "series-tvdb-88", "title": "Example series", "type": "series", "tvdb_id": "88",
     "library_ids": ["2"], "added_at": "2026-11-02T00:00:00Z", "versions": []},
    {"content_id": "episode-tvdb-88-0-2", "title": "Bonus", "type": "episode", "series_id": "series-tvdb-88",
     "series_title": "Example series", "season_number": 0, "episode_number": 2,
     "library_ids": ["2"], "added_at": "2026-11-03T00:00:00Z",
     "versions": [{"file_id": "22", "library_id": "2", "file_size": 2000000, "codec_video": "hevc"}]},
]


class NativeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        target = urlsplit(self.path)
        path, query = target.path.removeprefix("/api/v2"), parse_qs(target.query)
        self.server.requests.append((path, query, self.headers.get("Authorization")))
        status = 200
        if path != "/system/identity" and self.headers.get("Authorization") != "Bearer silo-secret":
            status, data = 401, {"error": "bad auth"}
        elif path in self.server.failures:
            status, data = self.server.failures[path], {"error": "silo-secret must not leak"}
        elif path == "/system/identity":
            data = {"server_id": "stable-deployment-id"}
        elif path == "/admin/sessions/capabilities":
            data = {"available": True}
        elif path == "/admin/catalog/capabilities":
            data = {"item_listing": True}
        elif path == "/user/libraries":
            data = {"items": [{"id": "1", "name": "Movies", "type": "movies"},
                              {"id": "2", "name": "TV", "type": "series"},
                              {"id": "3", "name": "Books", "type": "ebooks"}]}
        elif path in ("/admin/sessions", "/admin/users", "/admin/catalog/items"):
            rows = {"/admin/sessions": self.server.sessions, "/admin/users": USERS, "/admin/catalog/items": CATALOG}[path]
            if self.server.bad_shape:
                data = {"items": None}
            else:
                offset = int(query.get("cursor", ["0"])[0])
                more = offset + 1 < len(rows)
                data = {"items": rows[offset:offset + 1], "page": {"has_more": more}}
                if more:
                    data["page"]["next_cursor"] = "1" if self.server.loop_cursor else str(offset + 1)
        else:
            status, data = 404, {"error": "unexpected native route"}
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.native = ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler)
        self.native.sessions = [copy.deepcopy(SESSION)]
        self.native.failures, self.native.requests = {}, []
        self.native.bad_shape = self.native.loop_cursor = False
        self.config = Config(f"http://127.0.0.1:{self.native.server_port}", "silo-secret", "bridge-secret")
        self.bridge = Server(("127.0.0.1", 0), Bridge(self.config))
        for server in (self.native, self.bridge):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(self.stop, server, thread)

    @staticmethod
    def stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join()

    def request(self, path, headers=None, method="GET"):
        if headers is None:
            headers = {"Authorization": 'MediaBrowser Client="Tracearr", Token="bridge-secret"'}
        req = Request(f"http://127.0.0.1:{self.bridge.server_port}" + path, headers=headers, method=method)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_tracearr_connection_sequence(self):
        self.assertEqual(self.request("/System/Info/Public", {})[1]["Id"], "stable-deployment-id")
        self.assertEqual(self.request("/Users/Me")[0], 400)
        self.assertEqual(self.request("/Auth/Keys")[0], 200)
        self.assertEqual(self.request("/System/Info")[1]["ProductName"], "Silo Tracearr Bridge")
        self.assertEqual(self.request("/health/ready")[0], 200)
        self.assertTrue(all(auth == "Bearer silo-secret" for path, _, auth in self.native.requests if path != "/system/identity"))
        self.assertTrue(all(auth is None for path, _, auth in self.native.requests if path == "/system/identity"))

    def test_authentication_and_read_only_routes(self):
        for path in ("/Sessions", "/Users", "/Items", "/Auth/Keys", "/health/ready"):
            self.assertEqual(self.request(path, {})[0], 401)
            self.assertEqual(self.request(path, {"X-Emby-Token": "silo-secret"})[0], 401)
        self.assertEqual(self.request("/Sessions", {"X-Emby-Token": "bridge-secret"})[0], 200)
        self.assertEqual(self.request("/Sessions?api_key=bridge-secret", {})[0], 200)
        self.assertEqual(self.request("/Sessions", {"X-Emby-Authorization": 'MediaBrowser Token="bridge-secret"'})[0], 200)
        self.assertEqual(self.request("/Sessions/a/Playing/Stop", method="POST")[0], 405)
        self.assertEqual(self.request("/api/v2/admin/users")[0], 404)

    def test_paginated_users_libraries_and_identity_consistency(self):
        status, users = self.request("/Users")
        self.assertEqual(status, 200)
        self.assertEqual(len(users), 2)
        sessions = self.request("/Sessions")[1]
        self.assertEqual(sessions[0]["UserId"], users[0]["Id"])
        self.assertTrue(users[1]["Policy"]["IsDisabled"])
        self.assertEqual(self.request("/Users/" + users[0]["Id"])[1], users[0])
        libraries = self.request("/Library/VirtualFolders")[1]
        self.assertEqual(len(libraries), 2)
        self.assertEqual(libraries[1]["CollectionType"], "tvshows")
        self.assertEqual(users[1]["Policy"]["EnabledFolders"], [libraries[1]["ItemId"]])

    def test_live_native_play_pause_stop(self):
        result = self.request("/Sessions")[1][0]
        self.assertEqual(result["RemoteEndPoint"], "203.0.113.8")
        self.assertEqual(result["PlayState"]["PositionTicks"], 902500000)
        self.assertEqual(result["NowPlayingItem"]["RunTimeTicks"], 18000000000)
        self.assertEqual(result["NowPlayingItem"]["ParentIndexNumber"], 0)
        self.assertFalse(result["SupportsMediaControl"])
        self.native.sessions[0]["is_paused"] = True
        self.assertTrue(self.request("/Sessions")[1][0]["PlayState"]["IsPaused"])
        second = dict(SESSION, session_id="compat-play-43", user_id="8", username="bob", is_jellyfin_client=True)
        self.native.sessions.append(second)
        self.assertEqual(len(self.request("/Sessions")[1]), 2)
        self.native.sessions = []
        self.assertEqual(self.request("/Sessions"), (200, []))

    def test_upstream_failure_is_not_an_empty_session_list(self):
        self.native.failures["/admin/sessions"] = 403
        status, data = self.request("/Sessions")
        self.assertEqual(status, 502)
        self.assertIn("403", data["error"])
        self.assertNotIn("silo-secret", json.dumps(data))
        self.assertEqual(self.request("/health/live", {})[0], 200)

    def test_malformed_collection_and_cursor_loop_fail_closed(self):
        self.native.bad_shape = True
        self.assertEqual(self.request("/Sessions")[0], 502)
        self.native.bad_shape = False
        self.native.loop_cursor = True
        self.assertEqual(self.request("/Items")[0], 502)

    def test_catalog_pagination_filters_and_session_ids(self):
        library_id = resource_id("library", "2")
        result = self.request(f"/Items?ParentId={library_id}&IncludeItemTypes=Series&StartIndex=0&Limit=1")[1]
        self.assertEqual(result["TotalRecordCount"], 1)
        self.assertEqual(result["Items"][0]["Type"], "Series")
        result = self.request(f"/Items?ParentId={library_id}&IncludeItemTypes=Episode,Season")[1]
        episode = result["Items"][0]
        playing = self.request("/Sessions")[1][0]
        self.assertEqual(episode["Id"], playing["NowPlayingItem"]["Id"])
        self.assertEqual(episode["MediaSources"][0]["Id"], playing["PlayState"]["MediaSourceId"])
        result = self.request("/Items?Ids=" + episode["Id"])[1]
        self.assertEqual(result["TotalRecordCount"], 1)
        result = self.request("/Items?SortBy=DateCreated&SortOrder=Descending&Limit=1&StartIndex=1")[1]
        self.assertEqual(result["TotalRecordCount"], 3)
        self.assertEqual(result["Items"][0]["Type"], "Series")
        result = self.request("/Items?minDateLastSaved=2026-11-03T00:00:00Z")[1]
        self.assertEqual(result["TotalRecordCount"], 1)
        self.assertEqual(self.request("/Items?StartIndex=-1")[0], 400)
        self.assertEqual(self.request("/Items?Ids=invalid")[0], 400)

    def test_optional_features_are_explicitly_unsupported(self):
        self.assertEqual(self.request("/api/sse/events")[0], 404)
        self.assertEqual(self.request("/Plugins")[1], [])
        self.assertEqual(self.request("/Items?Filters=IsPlayed")[0], 501)
        self.assertEqual(self.request("/Users/user-Nw/Items")[0], 501)


class MappingTests(unittest.TestCase):
    def test_transcode_and_remux_decisions_and_units(self):
        for method, expected in [("transcode", "Transcode"), ("direct_stream", "Transcode"), ("remux", "DirectStream")]:
            row = dict(SESSION, effective_play_method=method, transcode_audio=method != "remux",
                       stream_bitrate_kbps=4000, target_video_codec="h264", target_audio_codec="aac")
            result = session(row)
            self.assertEqual(result["PlayState"]["PlayMethod"], expected)
            self.assertEqual(result["TranscodingInfo"]["Bitrate"], 4000000)
            self.assertEqual(result["TranscodingInfo"]["IsVideoDirect"], method != "transcode")
            self.assertEqual(result["TranscodingInfo"]["IsAudioDirect"], method == "remux")
            self.assertEqual(result["NowPlayingItem"]["MediaStreams"][0]["Height"], 1600)

    def test_ids_survive_restart_and_preserve_case(self):
        for value in ("7", "movie-tmdb-44", "custom:Épisode/ABC"):
            self.assertEqual(native_id("item", resource_id("item", value)), value)
        with self.assertRaises(ValueError):
            native_id("item", resource_id("user", "7"))
        with self.assertRaises(ValueError):
            native_id("item", "item-%%")

    def test_missing_metadata_is_not_fabricated(self):
        item = catalog_item(CATALOG[0])
        self.assertNotIn("RunTimeTicks", item)
        self.assertNotIn("Path", item["MediaSources"][0])
        self.assertNotIn("Height", item["MediaSources"][0]["MediaStreams"][0])
        row = dict(SESSION, file_duration=None)
        row.pop("device_id")
        result = session(row)
        self.assertNotIn("DeviceId", result)
        self.assertNotIn("RunTimeTicks", result["NowPlayingItem"])

    def test_configuration_rejects_missing_or_reused_secrets(self):
        for url, silo, bridge in [("file:///etc/passwd", "a", "b"), ("http://silo", "", "b"),
                                   ("http://silo", "a", "a"), ("http://a:b@silo", "a", "b")]:
            with self.assertRaises(ValueError):
                Config(url, silo, bridge)


if __name__ == "__main__":
    unittest.main()
