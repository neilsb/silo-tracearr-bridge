"""HTTP integration tests using an independent, paginated fake native Silo API."""

import copy
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from silo_tracearr.bridge import Bridge
from silo_tracearr.config import Config
from silo_tracearr.mapping import catalog_item, native_id, resource_id, session
from silo_tracearr.server import Server
from silo_tracearr.artwork import Artwork


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

    def do_POST(self):
        path = self.path.removeprefix("/api/v2")
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.server.writes.append((path, data, self.headers.get("Authorization")))
        status = self.server.failures.get(path, 200)
        session_id = path.split("/")[-2]
        if self.headers.get("Authorization") != "Bearer silo-secret":
            status = 401
        receipt = {"session_id": session_id, "authority_revoked": self.server.revoke,
                   "durable_state": "stopped", "client_notified": False, "delivery": "unavailable"}
        if status != 200:
            receipt = {"error": "upstream secret must not leak"}
        elif self.server.revoke:
            self.server.sessions = [s for s in self.server.sessions if s["session_id"] != session_id]
        body = json.dumps(receipt).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        target = urlsplit(self.path)
        path, query = target.path.removeprefix("/api/v2"), parse_qs(target.query)
        self.server.requests.append((path, query, self.headers.get("Authorization")))
        if path == "/artwork/local/poster.webp":
            self.send_response(200)
            self.send_header("Content-Type", self.server.artwork_type)
            self.send_header("Content-Length", "12")
            self.end_headers()
            self.wfile.write(b"RIFFtestWEBP")
            return
        status = 200
        if path != "/system/identity" and self.headers.get("Authorization") != "Bearer silo-secret":
            status, data = 401, {"error": "bad auth"}
        elif path in self.server.failures:
            status, data = self.server.failures[path], {"error": "silo-secret must not leak"}
        elif path == "/system/identity":
            data = {"server_id": "stable-deployment-id"}
        elif path == "/admin/sessions/capabilities":
            data = {"available": True}
        elif path == "/admin/sessions/command-capabilities":
            # Termination is independent of sequenced player command support.
            data = {"available": False, "actions": ["terminate"] if self.server.controls else [],
                    "terminate_revokes_authority": self.server.controls}
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
        self.native.artwork_type = "image/webp"
        self.native.controls = self.native.revoke = True
        self.native.writes = []
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

    def request(self, path, headers=None, method="GET", body=None):
        if headers is None:
            headers = {"Authorization": 'MediaBrowser Client="Tracearr", Token="bridge-secret"'}
        if body is not None:
            headers = dict(headers, **{"Content-Type": "application/json"})
        req = Request(f"http://127.0.0.1:{self.bridge.server_port}" + path, headers=headers, method=method,
                      data=json.dumps(body).encode() if body is not None else None)
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
        self.assertEqual(self.request("/Sessions/a/Playing/Pause", method="POST")[0], 405)
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
        self.assertTrue(result["SupportsMediaControl"])
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

    def test_tracearr_terminate_sequence_revokes_without_player_control(self):
        result = self.request("/Sessions")[1][0]
        self.assertTrue(result["SupportsMediaControl"])
        path = "/Sessions/" + result["Id"]
        self.assertEqual(self.request(path + "/Message", method="POST",
                                      body={"Text": "Please stop sharing", "Header": "Stream Terminated"})[0], 202)
        self.assertEqual(self.native.writes, [])  # Reason delivered with terminate, not as an unrelated command.
        status, receipt = self.request(path + "/Playing/Stop", method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(receipt["authority_revoked"])
        self.assertFalse(receipt["client_notified"])
        self.assertEqual(self.native.writes, [("/admin/sessions/native-play-42/terminate",
                                             {"reason": "Please stop sharing"}, "Bearer silo-secret")])
        self.assertEqual(self.request("/Sessions")[1], [])

    def test_control_authentication_and_route_boundaries(self):
        for action in ("Message", "Playing/Stop"):
            self.assertEqual(self.request("/Sessions/native-play-42/" + action, {}, "POST")[0], 401)
        for path in ("/api/v2/admin/users", "/Sessions/../Playing/Stop", "/Sessions/a%2fb/Playing/Stop"):
            self.assertEqual(self.request(path, method="POST")[0], 405)
        for body in (["not an object"], {"Text": 1}, {"Text": "x" * 1025}):
            self.assertEqual(self.request("/Sessions/native-play-42/Message", method="POST", body=body)[0], 400)
        self.assertEqual(self.native.writes, [])

    def test_unsupported_controls_do_not_break_monitoring(self):
        self.native.controls = False
        self.assertFalse(self.request("/Sessions")[1][0]["SupportsMediaControl"])
        self.assertEqual(self.request("/Sessions/native-play-42/Playing/Stop", method="POST")[0], 501)
        self.assertEqual(self.native.writes, [])

    def test_termination_failure_is_not_reported_as_success(self):
        path = "/Sessions/native-play-42/Playing/Stop"
        upstream = "/admin/sessions/native-play-42/terminate"
        for status, expected in [(403, 403), (404, 404), (409, 409), (503, 502)]:
            self.native.failures[upstream] = status
            response, body = self.request(path, method="POST")
            self.assertEqual(response, expected)
            self.assertNotIn("upstream secret", str(body))
        self.native.failures.clear()
        self.native.revoke = False
        self.assertEqual(self.request(path, method="POST")[0], 502)
        self.assertEqual(len(self.native.sessions), 1)

    def test_stale_termination_reason_is_not_reused(self):
        self.bridge.bridge.reasons["native-play-42"] = (time.monotonic() - 1, "old reason")
        self.assertEqual(self.request("/Sessions/native-play-42/Playing/Stop", method="POST")[0], 200)
        self.assertEqual(self.native.writes[0][1], {"reason": "Stream terminated by Tracearr"})

    def test_playback_poster_authentication_and_binary_delivery(self):
        self.native.sessions[0]["poster_url"] = "/api/v2/artwork/local/poster.webp?signature=secret"
        item = self.request("/Sessions")[1][0]["NowPlayingItem"]
        path = f'/Items/{item["Id"]}/Images/Primary?tag={item["ImageTags"]["Primary"]}&maxWidth=300'
        self.assertEqual(self.request(path, {})[0], 401)
        req = Request(f"http://127.0.0.1:{self.bridge.server_port}" + path,
                      headers={"X-Emby-Token": "bridge-secret"})
        with urlopen(req) as response:
            self.assertEqual(response.headers.get_content_type(), "image/webp")
            self.assertEqual(response.read(), b"RIFFtestWEBP")
        fetched = [x for x in self.native.requests if x[0] == "/artwork/local/poster.webp"]
        self.assertEqual(len(fetched), 1)
        self.assertIsNone(fetched[0][2])  # No Silo or bridge key on signed artwork requests.
        self.native.sessions = []
        with urlopen(req) as response:
            self.assertEqual(response.read(), b"RIFFtestWEBP")  # Retained after playback stops.
        self.assertEqual(len([x for x in self.native.requests if x[0] == "/artwork/local/poster.webp"]), 1)

    def test_playback_poster_recovers_registration_and_rejects_html(self):
        self.native.sessions[0]["poster_url"] = "/api/v2/artwork/local/poster.webp"
        self.native.artwork_type = "text/html"
        path = f'/Items/{resource_id("item", SESSION["content_id"])}/Images/Primary'
        status, body = self.request(path)
        self.assertEqual(status, 502)
        self.assertIn("content type", body["error"])
        self.assertEqual(self.request("/Items/nonexistent/Images/Primary")[0], 404)


class MappingTests(unittest.TestCase):
    def test_artwork_only_accepts_silo_artwork_and_stable_tags(self):
        artwork = Artwork(Config("https://silo.example", "silo-key", "bridge-key"))
        a = artwork.register("item-1", "/api/v2/artwork/local/poster.webp?signature=a")
        b = artwork.register("item-1", "/api/v2/artwork/local/poster.webp?signature=b")
        self.assertEqual(a, b)
        self.assertIsNotNone(a)
        for url in ("https://other.example/api/v2/artwork/local/p.webp", "//other.example/p.webp",
                    "/api/v2/admin/users", "/api/v2/artwork/../../admin/users", "/api/v2/artwork/%2e%2e/admin"):
            self.assertIsNone(artwork.register("item-1", url))

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

    def test_effective_audio_transcode_is_authoritative_without_legacy_flag(self):
        row = dict(SESSION, effective_play_method="direct_stream")
        self.assertFalse(session(row)["TranscodingInfo"]["IsAudioDirect"])

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
