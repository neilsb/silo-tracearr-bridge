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
    parser.add_argument("--go", action="store_true", help="Fetch fixtures through the native Go bridge instead of Python mappings")
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
assert.ok(sessions[0].media.thumbPath.includes('/Images/Primary'));
assert.equal(sessions[0].media.thumbPath, `/Items/${sessions[0].mediaId}/Images/Primary`);
assert.equal(sessions[0].quality.bitrate, 12000);
assert.equal(sessions[0].quality.isTranscode, false);
assert.equal(sessions[1].quality.isTranscode, true);
assert.equal(sessions[1].quality.videoDecision, 'transcode');
assert.equal(sessions[2].quality.videoDecision, 'copy');
assert.equal(sessions[2].quality.audioDecision, 'transcode');
assert.equal(sessions[3].quality.isTranscode, false);
if (input.richPlayback) {
  assert.equal(sessions[1].quality.videoWidth, 3840);
  assert.equal(sessions[1].quality.videoHeight, 1600);
  assert.equal(sessions[1].quality.streamVideoDetails.width, 1920);
  assert.equal(sessions[1].quality.streamVideoDetails.height, 800);
  assert.equal(sessions[1].quality.sourceAudioDetails.channelLayout, '5.1');
  assert.equal(sessions[1].quality.transcodeInfo.hwEncoding, 'qsv');
  assert.equal(sessions[2].quality.streamVideoDetails.width, 3840);
  assert.equal(sessions[2].quality.streamVideoDetails.height, 1600);
  assert.equal(sessions[2].quality.transcodeInfo.hwEncoding, undefined);
}
const users = parseUsersResponse(input.users);
assert.equal(users[0].id, sessions[0].user.id);
assert.equal(users[1].isDisabled, true);
const libraries = parseLibrariesResponse(input.libraries);
assert.equal(libraries[0].type, 'tvshows');
const items = parseLibraryItemsResponse(input.items);
assert.equal(items.length, 3);
assert.ok(items.some(item => item.ratingKey === sessions[0].mediaId));
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
        payload["sessions"][0]["NowPlayingItem"]["ImageTags"] = {"Primary": "test-image-tag"}
        if args.go:
            from verify_go_bridge import GoBridgeTests
            fixture = GoBridgeTests("test_tracearr_connection_sequence")
            try:
                fixture.setUp()
                fixture.native.sessions = [dict(SESSION, session_id=f"session-{index}", effective_play_method=method,
                                                transcode_audio=method in ("transcode", "direct_stream"),
                                                source_video_resolution="3840x1600", source_video_height=1600,
                                                source_audio_layout="5.1", target_resolution="1920x800",
                                                transcode_hw_accel="qsv",
                                                poster_url="/api/v2/artwork/local/poster.webp")
                                           for index, method in enumerate(("direct", "transcode", "direct_stream", "remux"))]
                def get(path):
                    status, result = fixture.request(path)
                    if status != 200:
                        raise RuntimeError(f"Go bridge returned HTTP {status} for {path}")
                    return result
                payload = {"richPlayback": True, "sessions": get("/Sessions"), "users": get("/Users"),
                           "libraries": [x for x in get("/Library/VirtualFolders") if x["CollectionType"] == "tvshows"],
                           "items": get("/Items")["Items"]}
            finally:
                fixture.doCleanups()
        subprocess.run(["node", str(output)], input=json.dumps(payload), text=True, check=True)


if __name__ == "__main__":
    main()
