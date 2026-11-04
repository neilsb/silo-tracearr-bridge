"""Translate native observations into the Jellyfin fields Tracearr consumes."""

import base64


def resource_id(kind, value):
    """Reversible, stable IDs; deliberately independent of Silo's compat IDs."""
    encoded = base64.urlsafe_b64encode(str(value).encode()).decode().rstrip("=")
    return f"{kind}-{encoded}"


def native_id(kind, value):
    prefix = kind + "-"
    if not value.startswith(prefix):
        raise ValueError("Invalid resource ID")
    encoded = value[len(prefix):]
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeError):
        raise ValueError("Invalid resource ID") from None
    if not decoded or resource_id(kind, decoded) != value:
        raise ValueError("Invalid resource ID")
    return decoded


def compact(data):
    return {k: v for k, v in data.items() if v is not None and v != ""}


def bps(value):
    return round(value * 1000) if value is not None else None


def height(resolution):
    # A size class is not an exact pixel height. Only use explicit dimensions.
    if isinstance(resolution, str) and "x" in resolution.lower():
        try:
            return int(resolution.lower().split("x")[1])
        except ValueError:
            pass
    return None


def user(row):
    folders = row.get("effective_policy", {}).get("library_ids")
    return compact({
        "Id": resource_id("user", row["id"]), "Name": row["username"],
        "Policy": {"IsAdministrator": row.get("role") == "admin",
                   "IsDisabled": not row.get("enabled", True),
                   "EnableAllFolders": folders is None,
                   "EnabledFolders": [resource_id("library", x) for x in (folders or [])]},
        "LastActivityDate": row.get("last_active_at"),
    })


def library(row):
    return {"ItemId": resource_id("library", row["id"]), "Name": row["name"],
            "CollectionType": {"series": "tvshows", "movies": "movies"}.get(row["type"], "mixed"),
            "Locations": []}


def catalog_item(row):
    sources = []
    for version in row.get("versions", []):
        streams = []
        if version.get("codec_video"):
            streams.append(compact({"Type": "Video", "Codec": version["codec_video"],
                                    "Height": height(version.get("resolution")),
                                    "VideoRangeType": {"sdr": "SDR", "hdr10": "HDR10", "hdr10_plus": "HDR10Plus",
                                                       "hlg": "HLG", "dolby_vision": "DOVI"}.get(version.get("dynamic_range"))}))
        if version.get("codec_audio"):
            streams.append(compact({"Type": "Audio", "Codec": version["codec_audio"],
                                    "Channels": version.get("audio_channels"),
                                    "Title": "Dolby Atmos" if version.get("atmos") else None}))
        sources.append(compact({"Id": resource_id("file", version["file_id"]),
                                "Name": version.get("edition"), "Size": version.get("file_size"),
                                "Container": version.get("container"),
                                "Bitrate": bps(version.get("bitrate_kbps")), "MediaStreams": streams}))
    return compact({
        "Id": resource_id("item", row["content_id"]), "Name": row["title"],
        "Type": row["type"].title(), "IsFolder": row["type"] == "series",
        "LocationType": "FileSystem", "ProductionYear": row.get("year"),
        "DateCreated": row.get("added_at"), "MediaSources": sources,
        "ProviderIds": compact({"Tmdb": row.get("tmdb_id"), "Tvdb": row.get("tvdb_id"), "Imdb": row.get("imdb_id")}),
        "SeriesId": resource_id("item", row["series_id"]) if row.get("series_id") else None,
        "SeriesName": row.get("series_title"), "ParentIndexNumber": row.get("season_number"),
        "IndexNumber": row.get("episode_number"),
    })


def session(row):
    method = row.get("effective_play_method") or row["play_method"]
    play_method = {"direct": "DirectPlay", "remux": "DirectStream", "direct_stream": "Transcode",
                   "audio": "Transcode", "transcode": "Transcode"}.get(method)
    if play_method is None:
        raise ValueError("Unknown Silo playback method")
    streams = []
    if row.get("source_video_codec"):
        streams.append(compact({"Type": "Video", "Codec": row["source_video_codec"],
                                "Height": row.get("source_video_height") or height(row.get("source_video_resolution"))}))
    if row.get("source_audio_codec"):
        streams.append(compact({"Type": "Audio", "Codec": row["source_audio_codec"],
                                "Index": row.get("audio_track_index", 0),
                                "Channels": row.get("source_audio_channels"),
                                "Language": row.get("source_audio_language"), "Title": row.get("source_audio_title")}))
    file_id = resource_id("file", row["media_file_id"])
    item_id = resource_id("item", row["content_id"]) if row.get("content_id") else file_id
    media_type = {"movie": "Movie", "episode": "Episode", "audio": "Audio", "track": "Audio"}.get(row["media_type"], row["media_type"].title())
    item = compact({"Id": item_id, "Name": row.get("episode_name") or row["media_title"],
                    "Type": media_type, "RunTimeTicks": round(row["file_duration"] * 10_000_000) if row.get("file_duration") is not None else None,
                    "SeriesName": row.get("series_name"), "ParentIndexNumber": row.get("season_number"),
                    "IndexNumber": row.get("episode_number"), "MediaStreams": streams,
                    "MediaSources": [compact({"Id": file_id, "MediaStreams": streams,
                                              "Bitrate": bps(row.get("source_bitrate_kbps") or row.get("stream_bitrate_kbps")),
                                              "Container": row.get("source_container")})]})
    result = compact({
        "Id": row["session_id"], "UserId": resource_id("user", row["user_id"]), "UserName": row["username"],
        "Client": row.get("client_name"), "ApplicationVersion": row.get("client_version"),
        "DeviceId": row.get("device_id"), "DeviceName": row.get("client_label") or row.get("client_name"),
        "DeviceType": row.get("device_platform"), "RemoteEndPoint": row.get("client_ip"),
        "LastActivityDate": row["updated_at"], "LastPlaybackCheckIn": row["updated_at"],
        "IsActive": True, "SupportsMediaControl": False, "SupportsRemoteControl": False,
        "NowPlayingItem": item,
        "PlayState": {"PositionTicks": round(row["position_seconds"] * 10_000_000),
                      "IsPaused": row["is_paused"], "PlayMethod": play_method,
                      "MediaSourceId": file_id, "AudioStreamIndex": row.get("audio_track_index", 0)},
    })
    if method != "direct":
        result["TranscodingInfo"] = compact({
            "IsVideoDirect": row.get("video_decision") != "transcode" if row.get("video_decision") else method != "transcode",
            "IsAudioDirect": row.get("audio_decision") != "transcode" if row.get("audio_decision") else not row.get("transcode_audio", False),
            "VideoCodec": row.get("target_video_codec"), "AudioCodec": row.get("target_audio_codec"),
            "AudioChannels": row.get("target_audio_channels"), "Container": row.get("output_container"),
            "Bitrate": bps(row.get("stream_bitrate_kbps") or row.get("target_bitrate_kbps")),
            "Height": height(row.get("target_resolution")),
        })
    return result
