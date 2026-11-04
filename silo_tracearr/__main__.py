import argparse
import json
import logging
import sys

from .bridge import Bridge
from .client import UpstreamError
from .config import Config
from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="Present Silo monitoring as a Jellyfin API for Tracearr")
    parser.add_argument("--check", action="store_true", help="Check Silo connectivity/scopes without starting the bridge")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = Config.from_env()
        if args.check:
            bridge = Bridge(config)
            bridge.verify()
            sessions = bridge.get("/Sessions", {})
            print(json.dumps({"server": bridge.info(), "active_sessions": len(sessions),
                              "users": len(bridge.users()), "libraries": len(bridge.libraries())}, indent=2))
            return
        with make_server(config) as server:
            logging.info("Bridge listening on %s:%d", config.host, config.port)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (ValueError, UpstreamError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
