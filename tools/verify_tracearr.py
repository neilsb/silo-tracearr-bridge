"""Optional check against a Tracearr checkout's real TypeScript parsers.

Requires Node/npm; downloads pinned esbuild through npm exec. Does not modify
the Tracearr checkout or install its full workspace dependencies.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from silo_tracearr import mapping  # noqa: E402
from test_bridge import CATALOG, SESSION, USERS  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracearr", type=Path, help="Path to an existing Tracearr checkout")
    args = parser.parse_args()
    source = args.tracearr.resolve()
    with tempfile.TemporaryDirectory(prefix="tracearr-parser-") as temp:
        temp = Path(temp)
        shim = temp / "shared.ts"
        shim.write_text(
            f'export {{ classifyByDimensions, resolutionTierRank }} from {json.dumps(str(source / "packages/shared/src/resolution.ts"))};\n'
            f'export {{ normalizeDynamicRange }} from {json.dumps(str(source / "packages/shared/src/dynamicRange.ts"))};\n'
        )
        entry = temp / "check.ts"
        entry.write_text('''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseSessionsResponse, parseUsersResponse, parseLibrariesResponse, parseLibraryItemsResponse} from PARSER;
const input = JSON.parse(readFileSync(0, 'utf8'));
const sessions = parseSessionsResponse(input.sessions);
assert.equal(sessions.length, 4);
assert.equal(sessions[0].playback.positionMs, 90250);
assert.equal(sessions[0].media.durationMs, 1800000);
assert.equal(sessions[0].episode.seasonNumber, 0);
assert.equal(sessions[0].network.ipAddress, '203.0.113.8');
assert.equal(sessions[0].player.deviceId, 'living-room-tv');
assert.equal(sessions[0].quality.bitrate, 12000);
assert.equal(sessions[0].quality.isTranscode, false);
assert.equal(sessions[1].quality.isTranscode, true);
assert.equal(sessions[1].quality.videoDecision, 'transcode');
assert.equal(sessions[2].quality.videoDecision, 'copy');
assert.equal(sessions[2].quality.audioDecision, 'transcode');
assert.equal(sessions[3].quality.isTranscode, false);
const users = parseUsersResponse(input.users);
assert.equal(users[0].id, sessions[0].user.id);
assert.equal(users[1].isDisabled, true);
const libraries = parseLibrariesResponse(input.libraries);
assert.equal(libraries[0].type, 'tvshows');
const items = parseLibraryItemsResponse(input.items);
assert.equal(items.length, 3);
assert.equal(items[2].ratingKey, sessions[0].mediaId);
console.log('Tracearr parser contract passed: direct play, video/audio transcode, remux, users, libraries, catalog');
'''.replace("PARSER", json.dumps(str(source / "apps/server/src/services/mediaServer/jellyfin/parser.ts"))))
        output = temp / "check.cjs"
        subprocess.run(["npm", "exec", "--yes", "--package=esbuild@0.25.10", "--", "esbuild", str(entry),
                        "--bundle", "--platform=node", "--format=cjs", f"--outfile={output}",
                        f"--alias:@tracearr/shared={shim}"], check=True)
        payload = {
            "sessions": [mapping.session(dict(SESSION, effective_play_method=method,
                                              transcode_audio=method in ("transcode", "direct_stream")))
                         for method in ("direct", "transcode", "direct_stream", "remux")],
            "users": [mapping.user(row) for row in USERS],
            "libraries": [mapping.library({"id": "2", "name": "TV", "type": "series"})],
            "items": [mapping.catalog_item(row) for row in CATALOG],
        }
        subprocess.run(["node", str(output)], input=json.dumps(payload), text=True, check=True)


if __name__ == "__main__":
    main()
