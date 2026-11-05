package main

import "testing"

func TestPlaybackDetailsDoNotInventSourceOrHardware(t *testing.T) {
	row := object{"session_id": "s", "user_id": "u", "media_file_id": "f", "position_seconds": 0,
		"is_paused": false, "updated_at": "now", "effective_play_method": "transcode",
		"source_video_codec": "hevc", "source_video_resolution": "2160p", "source_video_height": 1600,
		"stream_bitrate_kbps": 4000, "target_resolution": "1920x800", "transcode_hw_accel": "none"}
	session, err := mapSession(row)
	if err != nil {
		t.Fatal(err)
	}
	source := obj(session["NowPlayingItem"])["MediaSources"].([]object)[0]
	if _, ok := source["Bitrate"]; ok {
		t.Fatal("output bitrate was presented as source bitrate")
	}
	video := source["MediaStreams"].([]object)[0]
	if _, ok := video["Width"]; ok {
		t.Fatal("invented width from resolution class")
	}
	if video["Height"] != 1600 {
		t.Fatal("lost actual cropped source height")
	}
	info := obj(session["TranscodingInfo"])
	if info["HardwareAccelerationType"] != "none" || info["Width"] != 1920 {
		t.Fatalf("incorrect output: %v", info)
	}
	row["effective_play_method"] = "remux"
	row["transcode_hw_accel"] = "qsv"
	session, err = mapSession(row)
	if err != nil {
		t.Fatal(err)
	}
	info = obj(session["TranscodingInfo"])
	if _, ok := info["HardwareAccelerationType"]; ok {
		t.Fatal("copied video reported hardware encoding")
	}
	if info["Height"] != 1600 || info["VideoCodec"] != "hevc" {
		t.Fatal("copy did not preserve source details")
	}
	for _, value := range []string{"2160p", "badx1080", "0x1080", "1920x0"} {
		if exactHeight(value) != nil || exactDimension(value, 0) != nil {
			t.Fatalf("invalid dimensions accepted: %s", value)
		}
	}
}
