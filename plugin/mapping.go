package main

import (
	"encoding/base64"
	"fmt"
	"math"
	"strconv"
	"strings"
)

func str(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func num(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	case int64:
		return float64(n)
	}
	return 0
}
func yes(v any) bool { b, _ := v.(bool); return b }
func obj(v any) object {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return object{}
}
func contains(v any, s string) bool {
	if list, ok := v.([]any); ok {
		for _, x := range list {
			if str(x) == s {
				return true
			}
		}
	}
	return false
}
func clean(m object) object {
	for k, v := range m {
		if v == nil || v == "" {
			delete(m, k)
		}
	}
	return m
}
func rid(kind string, v any) string {
	return kind + "-" + base64.RawURLEncoding.EncodeToString([]byte(str(v)))
}
func decodeID(kind, value string) (string, error) {
	if !strings.HasPrefix(value, kind+"-") {
		return "", fail(400, "invalid resource ID")
	}
	raw, e := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(value, kind+"-"))
	if e != nil || len(raw) == 0 || rid(kind, string(raw)) != value {
		return "", fail(400, "invalid resource ID")
	}
	return string(raw), nil
}
func first(vs ...any) any {
	for _, v := range vs {
		if v != nil && v != "" && v != float64(0) {
			return v
		}
	}
	return nil
}
func bitrate(v any) any {
	if v == nil {
		return nil
	}
	return math.Round(num(v) * 1000)
}
func exactHeight(v any) any {
	return exactDimension(v, 1)
}
func exactDimension(v any, axis int) any {
	parts := strings.Split(strings.ToLower(str(v)), "x")
	if len(parts) == 2 {
		width, ew := strconv.Atoi(parts[0])
		height, eh := strconv.Atoi(parts[1])
		if ew == nil && eh == nil && width > 0 && height > 0 {
			return []int{width, height}[axis]
		}
	}
	return nil
}
func mapUser(r object) object {
	folders := obj(r["effective_policy"])["library_ids"]
	enabled := []string{}
	if list, ok := folders.([]any); ok {
		for _, id := range list {
			enabled = append(enabled, rid("library", id))
		}
	}
	disabled := false
	if v, ok := r["enabled"]; ok {
		disabled = !yes(v)
	}
	return clean(object{"Id": rid("user", r["id"]), "Name": r["username"], "Policy": object{"IsAdministrator": r["role"] == "admin", "IsDisabled": disabled, "EnableAllFolders": folders == nil, "EnabledFolders": enabled}, "LastActivityDate": r["last_active_at"]})
}
func mapLibrary(r object) object {
	kind := str(r["type"])
	if kind == "series" {
		kind = "tvshows"
	}
	return object{"ItemId": rid("library", r["id"]), "Name": r["name"], "CollectionType": kind, "Locations": []string{}}
}
func mapItem(r object) object {
	sources := []object{}
	list, _ := r["versions"].([]any)
	for _, raw := range list {
		v := obj(raw)
		streams := []object{}
		if str(v["codec_video"]) != "" {
			streams = append(streams, clean(object{"Type": "Video", "Codec": v["codec_video"], "Height": exactHeight(v["resolution"]), "VideoRangeType": map[string]string{"sdr": "SDR", "hdr10": "HDR10", "hdr10_plus": "HDR10Plus", "hlg": "HLG", "dolby_vision": "DOVI"}[str(v["dynamic_range"])]}))
		}
		if str(v["codec_audio"]) != "" {
			audio := clean(object{"Type": "Audio", "Codec": v["codec_audio"], "Channels": v["audio_channels"]})
			if yes(v["atmos"]) {
				audio["Title"] = "Dolby Atmos"
			}
			streams = append(streams, audio)
		}
		sources = append(sources, clean(object{"Id": rid("file", v["file_id"]), "Name": v["edition"], "Size": v["file_size"], "Container": v["container"], "Bitrate": bitrate(v["bitrate_kbps"]), "MediaStreams": streams}))
	}
	seriesID := ""
	if str(r["series_id"]) != "" {
		seriesID = rid("item", r["series_id"])
	}
	kind := map[string]string{"movie": "Movie", "series": "Series", "episode": "Episode"}[str(r["type"])]
	return clean(object{"Id": rid("item", r["content_id"]), "Name": r["title"], "Type": kind, "IsFolder": kind == "Series", "LocationType": "FileSystem", "ProductionYear": r["year"], "DateCreated": r["added_at"], "MediaSources": sources, "ProviderIds": clean(object{"Tmdb": r["tmdb_id"], "Tvdb": r["tvdb_id"], "Imdb": r["imdb_id"]}), "SeriesId": seriesID, "SeriesName": r["series_title"], "ParentIndexNumber": r["season_number"], "IndexNumber": r["episode_number"]})
}
func mapSession(r object) (object, error) {
	for _, key := range []string{"session_id", "user_id", "media_file_id", "position_seconds", "is_paused", "updated_at"} {
		if r[key] == nil {
			return nil, fail(502, "incomplete Silo session")
		}
	}
	method := str(first(r["effective_play_method"], r["play_method"]))
	playMethod := map[string]string{"direct": "DirectPlay", "remux": "DirectStream", "direct_stream": "Transcode", "audio": "Transcode", "transcode": "Transcode"}[method]
	if playMethod == "" {
		return nil, fail(502, "unknown Silo playback method")
	}
	streams := []object{}
	if str(r["source_video_codec"]) != "" {
		streams = append(streams, clean(object{"Type": "Video", "Codec": r["source_video_codec"], "Width": exactDimension(r["source_video_resolution"], 0), "Height": first(r["source_video_height"], exactHeight(r["source_video_resolution"]))}))
	}
	if str(r["source_audio_codec"]) != "" {
		streams = append(streams, clean(object{"Type": "Audio", "Codec": r["source_audio_codec"], "Index": num(r["audio_track_index"]), "Channels": r["source_audio_channels"], "ChannelLayout": r["source_audio_layout"], "Language": r["source_audio_language"], "Title": r["source_audio_title"]}))
	}
	fileID := rid("file", r["media_file_id"])
	itemID := fileID
	if str(r["content_id"]) != "" {
		itemID = rid("item", r["content_id"])
	}
	kind := map[string]string{"movie": "Movie", "episode": "Episode", "audio": "Audio", "track": "Audio"}[str(r["media_type"])]
	if kind == "" {
		kind = str(r["media_type"])
	}
	sourceBitrate := r["source_bitrate_kbps"]
	if method == "direct" {
		sourceBitrate = first(sourceBitrate, r["stream_bitrate_kbps"])
	}
	item := clean(object{"Id": itemID, "Name": first(r["episode_name"], r["media_title"]), "Type": kind, "SeriesName": r["series_name"], "ParentIndexNumber": r["season_number"], "IndexNumber": r["episode_number"], "MediaStreams": streams, "MediaSources": []object{clean(object{"Id": fileID, "MediaStreams": streams, "Bitrate": bitrate(sourceBitrate), "Container": r["source_container"]})}})
	if r["file_duration"] != nil {
		item["RunTimeTicks"] = math.Round(num(r["file_duration"]) * 10000000)
	}
	result := clean(object{"Id": r["session_id"], "UserId": rid("user", r["user_id"]), "UserName": r["username"], "Client": r["client_name"], "ApplicationVersion": r["client_version"], "DeviceId": r["device_id"], "DeviceName": first(r["client_label"], r["client_name"]), "DeviceType": r["device_platform"], "RemoteEndPoint": r["client_ip"], "LastActivityDate": r["updated_at"], "LastPlaybackCheckIn": r["updated_at"], "IsActive": true, "SupportsMediaControl": false, "SupportsRemoteControl": false, "NowPlayingItem": item, "PlayState": object{"PositionTicks": math.Round(num(r["position_seconds"]) * 10000000), "IsPaused": r["is_paused"], "PlayMethod": playMethod, "MediaSourceId": fileID, "AudioStreamIndex": num(r["audio_track_index"])}})
	if method != "direct" {
		videoDirect := method != "transcode"
		if str(r["video_decision"]) != "" {
			videoDirect = r["video_decision"] != "transcode"
		}
		audioDirect := !yes(r["transcode_audio"]) && method != "audio" && method != "direct_stream"
		if str(r["audio_decision"]) != "" {
			audioDirect = r["audio_decision"] != "transcode"
		}
		result["TranscodingInfo"] = clean(object{"IsVideoDirect": videoDirect, "IsAudioDirect": audioDirect, "VideoCodec": r["target_video_codec"], "AudioCodec": r["target_audio_codec"], "AudioChannels": r["target_audio_channels"], "Container": r["output_container"], "Bitrate": bitrate(first(r["stream_bitrate_kbps"], r["target_bitrate_kbps"])), "Height": exactHeight(r["target_resolution"])})
		info := obj(result["TranscodingInfo"])
		info["Width"] = exactDimension(r["target_resolution"], 0)
		if videoDirect {
			info["VideoCodec"] = r["source_video_codec"]
			info["Width"] = exactDimension(r["source_video_resolution"], 0)
			info["Height"] = first(r["source_video_height"], exactHeight(r["source_video_resolution"]))
		} else {
			// Silo reports the effective encoder accelerator, not decoder hardware.
			info["HardwareAccelerationType"] = r["transcode_hw_accel"]
		}
		if audioDirect {
			info["AudioCodec"] = r["source_audio_codec"]
			info["AudioChannels"] = r["source_audio_channels"]
		}
		clean(info)
	}
	return result, nil
}
