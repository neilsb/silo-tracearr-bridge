"""Authenticated HTTP transport. No arbitrary upstream proxying."""

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from .bridge import APIError, Bridge
from .client import UpstreamError
from .artwork import Image

log = logging.getLogger(__name__)


def request_token(headers, query):
    for key in ("Authorization", "X-Emby-Authorization"):
        value = headers.get(key, "")
        match = re.search(r'(?:^|[ ,])Token="([^"\r\n]+)"', value, re.IGNORECASE)
        if match:
            return match[1]
    return headers.get("X-Emby-Token") or query.get("api_key") or query.get("apikey") or ""


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, bridge):
        self.bridge = bridge
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "SiloTracearrBridge/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        # Default logging prints query strings, which may include API keys.
        pass

    def reply(self, status, data):
        body = data.body if isinstance(data, Image) else json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", data.content_type if isinstance(data, Image) else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            target = urlsplit(self.path)
            path = target.path
            if len(self.path) > 16384:
                raise APIError(414, "Request target too long")
            query = {key.lower(): value for key, value in parse_qsl(target.query, max_num_fields=100)}
            if path.lower().rstrip("/") not in ("/health/live", "/system/info/public"):
                token = request_token(self.headers, query)
                if not hmac.compare_digest(token.encode(), self.server.bridge.config.bridge_api_key.encode()):
                    raise APIError(401, "Invalid bridge API key")
            query.pop("api_key", None)
            query.pop("apikey", None)
            data = self.server.bridge.get(path, query)
            self.reply(200, data)
        except APIError as error:
            self.reply(error.status, {"error": str(error)})
        except UpstreamError as error:
            log.warning("Upstream request failed: %s", error)
            self.reply(502, {"error": str(error)})
        except (KeyError, TypeError, ValueError):
            log.warning("Request or upstream data did not match the expected contract")
            self.reply(502, {"error": "Request or Silo response did not match the bridge contract"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_POST(self):
        self.reply(405, {"error": "This bridge is read-only"})

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def make_server(config):
    return Server((config.host, config.port), Bridge(config))
