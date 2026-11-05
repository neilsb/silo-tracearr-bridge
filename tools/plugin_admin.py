"""Install/configure/check the native plugin using Silo's administrator API.

Credentials come from SILO_URL/SILO_API_KEY/BRIDGE_API_KEY, or --from-compose.
Secrets and raw installation/configuration responses are never printed.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-compose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("binary", type=Path)
    install.add_argument("--replace", action="store_true", help="Replace this plugin's existing installation")
    configure = commands.add_parser("configure")
    configure.add_argument("id", type=int)
    configure.add_argument("--native-url", default="http://127.0.0.1:8080")
    configure.add_argument("--listen", default="0.0.0.0:8098")
    status = commands.add_parser("status")
    status.add_argument("id", type=int)
    recovery = commands.add_parser("recovery", help="Keep startup and add a 60-second recovery task trigger")
    recovery.add_argument("id", type=int)
    args = parser.parse_args()
    env = os.environ
    if args.from_compose:
        env = json.loads(subprocess.check_output(["docker", "compose", "config", "--format", "json"]))["services"]["silo-tracearr-bridge"]["environment"]
    base, key = env["SILO_URL"].rstrip("/"), env["SILO_API_KEY"]

    response_headers = {}
    def request(method, path, data=None, content_type="application/json", extra_headers=None):
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        headers.update(extra_headers or {})
        if data is not None:
            headers["Content-Type"] = content_type
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
        try:
            with build_opener(NoRedirects).open(Request(base + path, data=data, headers=headers, method=method), timeout=90) as response:
                response_headers.clear()
                response_headers.update(response.headers)
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            code = error.code
            raw = error.read(8192).decode(errors="replace")
            error.close()
            for secret in (key, env.get("BRIDGE_API_KEY", "")):
                if secret:
                    raw = raw.replace(secret, "[REDACTED]")
            raise RuntimeError(f"Silo HTTP {code}: {raw}") from None

    if args.command == "install":
        manifest = json.loads(subprocess.check_output([str(args.binary.resolve()), "manifest"]))
        plugin_id = manifest.get("pluginId", manifest.get("plugin_id"))
        if plugin_id != "community.tracearr-bridge":
            raise RuntimeError("This tool only installs community.tracearr-bridge")
        existing = request("GET", "/api/v2/admin/plugins/installations").get("items", [])
        if any(x.get("plugin_id") == plugin_id for x in existing) and not args.replace:
            raise RuntimeError("Plugin is already installed; use --replace explicitly to update it")
        boundary = "bridge-" + uuid.uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="archive"; filename="tracearr-bridge-linux-amd64"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                + args.binary.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
        result = request("POST", "/api/v2/admin/plugins/uploads", data, "multipart/form-data; boundary=" + boundary)
        print(json.dumps({k: result.get(k) for k in ("id", "plugin_id", "version", "enabled")}, indent=2))
    elif args.command == "configure":
        value = {"silo_url": args.native_url, "silo_api_key": key,
                 "bridge_api_key": env["BRIDGE_API_KEY"], "listen_address": args.listen,
                 "server_name": env.get("BRIDGE_NAME", "Silo (Tracearr bridge)")}
        request("PUT", f"/api/v2/admin/plugins/installations/{args.id}/config", {"key": "bridge", "value": value})
        print("Plugin settings saved. Run status to start and verify the listener.")
    elif args.command == "recovery":
        path = f"/api/v2/admin/tasks/plugin:{args.id}:listener/triggers"
        schedule = request("GET", path)
        tag = next((v for k, v in response_headers.items() if k.lower() == "etag"), None)
        if not tag:
            raise RuntimeError("Silo did not provide a schedule ETag")
        triggers = schedule.get("triggers", [])
        if not any(t.get("type") == "startup" for t in triggers):
            triggers.append({"type": "startup"})
        if not any(t.get("type") == "interval" and 0 < t.get("interval_ms", 0) <= 60000 for t in triggers):
            triggers.append({"type": "interval", "interval_ms": 60000})
        request("PUT", path, {"triggers": triggers}, extra_headers={"If-Match": tag})
        saved = request("GET", path)
        print(json.dumps(saved, indent=2))
    else:
        result = request("GET", f"/api/v2/plugin-content/plugins/{args.id}/status")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
