
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MHD Sports - Sports API updater
Supports:
- GET / POST APIs
- ChuoiChienTV
- GaVangTV
- PLAPI Graph API + live resolver
- M3U / XMLTV / sources.json
- Configurable stream headers and Referer
"""

import os
import re
import hashlib
import json
import time
import html
import logging
import ssl
import certifi
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


# ============================================================
# PATHS AND LOGGING
# ============================================================

VN_TZ = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="[%(levelname)s] %(message)s"
)

log = logging.getLogger("MHD-Sports")


# ============================================================
# CONFIGURATION
# ============================================================

def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Không tìm thấy config.json: {CONFIG_FILE}"
        )

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
TIMEOUT = int(CONFIG.get("request_timeout_seconds", 25))
RETRIES = int(CONFIG.get("request_retries", 2))

# SSL certificate configuration
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

OUTPUT_FILE = ROOT / CONFIG.get("output_file", "thethao.m3u")
EPG_FILE = ROOT / CONFIG.get("epg_file", "epg.xml")
SOURCES_FILE = ROOT / CONFIG.get("sources_file", "sources.json")

CACHE_FILE = ROOT / "referer_cache.json"

INCLUDE_GROUPS = CONFIG.get(
    "include_stream_groups",
    ["blvs", "blvs_bonglau", "blvs_nguoitho"]
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean(value):
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return ""

    return str(value).strip()


def safe_filename(value):
    value = clean(value).lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    return value.strip("-") or "source"


def first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def parse_datetime(value):
    if value is None or value == "":
        return None

    try:
        # Hỗ trợ Unix timestamp dạng số hoặc chuỗi số.
        if isinstance(value, (int, float)):
            timestamp = float(value)

        elif isinstance(value, str):
            value = value.strip()

            if re.fullmatch(r"\d{9,16}", value):
                timestamp = float(value)
            else:
                timestamp = None

        else:
            timestamp = None

        if timestamp is not None:
            # Tự nhận diện giây hoặc mili giây.
            if timestamp > 9999999999:
                timestamp /= 1000

            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            )

        # Xử lý chuỗi ISO 8601.
        if isinstance(value, str):
            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt

    except (ValueError, TypeError, OverflowError, OSError):
        pass

    # Các định dạng ngày giờ phổ biến.
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M"
    ):
        try:
            return datetime.strptime(
                str(value).strip(),
                fmt
            ).replace(tzinfo=timezone.utc)

        except (ValueError, TypeError):
            continue

    return None


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_suffix(path.suffix + ".tmp")

    with open(temp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    os.replace(temp, path)


def atomic_write_json(path, data):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2)
    )


# ============================================================
# HTTP CLIENT
# ============================================================

def retry_delay(response_headers=None, attempt=0):
    if response_headers:
        retry_after = response_headers.get("Retry-After")

        if retry_after:
            try:
                return min(int(retry_after), 30)
            except Exception:
                try:
                    date = parsedate_to_datetime(retry_after)
                    seconds = (
                        date - datetime.now(timezone.utc)
                    ).total_seconds()

                    return max(1, min(int(seconds), 30))
                except Exception:
                    pass

    return min(2 ** attempt, 15)


def request_json(
    url,
    method="GET",
    headers=None,
    params=None,
    body=None,
    timeout=None
):
    method = clean(method).upper() or "GET"

    headers = dict(headers or {})
    headers.setdefault("User-Agent", "MHD-Sports/3.0")
    headers.setdefault("Accept", "application/json")

    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        separator = "&" if "?" in url else "?"
        url = url + separator + query

    data = None

    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            headers.setdefault(
                "Content-Type",
                "application/json"
            )
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body

    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method=method
            )

            with urllib.request.urlopen(
                req,
                timeout=timeout or TIMEOUT,
                context=SSL_CONTEXT
            ) as response:
                raw = response.read().decode(
                    "utf-8",
                    errors="replace"
                )

                if not raw.strip():
                    return None

                return json.loads(raw)

        except urllib.error.HTTPError as exc:
            status = exc.code

            try:
                error_body = exc.read().decode(
                    "utf-8",
                    errors="replace"
                )
            except Exception:
                error_body = ""

            retryable = status in (
                408, 425, 429, 500, 502, 503, 504
            )

            if retryable and attempt < RETRIES:
                delay = retry_delay(
                    exc.headers,
                    attempt
                )

                log.warning(
                    "HTTP %s, thử lại sau %ss: %s",
                    status,
                    delay,
                    url
                )

                time.sleep(delay)
                continue

            raise RuntimeError(
                f"HTTP {status}: {error_body[:300]}"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError
        ) as exc:
            if attempt < RETRIES:
                delay = retry_delay(
                    attempt=attempt
                )

                log.warning(
                    "Lỗi kết nối, thử lại sau %ss: %s",
                    delay,
                    url
                )

                time.sleep(delay)
                continue

            raise RuntimeError(str(exc))

        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"API trả về JSON không hợp lệ: {exc}"
            )

    raise RuntimeError("Không thể kết nối API")


# ============================================================
# DATA EXTRACTION
# ============================================================

def unwrap_data(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in ("matches", "items", "results"):
        value = payload.get(key)

        if isinstance(value, list):
            return value

    data = payload.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("matches", "items", "results"):
            value = data.get(key)

            if isinstance(value, list):
                return value

        return list(data.values())

    return []


def get_nested(obj, *keys):
    current = obj

    for key in keys:
        if not isinstance(current, dict):
            return None

        current = current.get(key)

    return current


# ============================================================
# STREAM NORMALIZATION
# ============================================================

def normalize_stream(stream, default_referer=""):
    if isinstance(stream, str):
        url = stream
        label = "Live"
        headers = {}

    elif isinstance(stream, dict):
        url = first_value(
            stream.get("url"),
            stream.get("link"),
            stream.get("src"),
            stream.get("source"),
            stream.get("play_url"),
            stream.get("playUrl")
        )

        label = first_value(
            stream.get("label"),
            stream.get("name"),
            stream.get("quality"),
            stream.get("title"),
            "Live"
        )

        headers = stream.get("headers") or {}

    else:
        return None

    url = clean(url)

    if not url.startswith(("http://", "https://")):
        return None

    headers = dict(headers)

    if default_referer and not any(
        k.lower() == "referer" for k in headers
    ):
        headers["Referer"] = default_referer

    return {
        "url": url,
        "label": clean(label),
        "headers": headers
    }


def extract_streams(item, default_referer=""):
    streams = []

    possible_keys = [
        "streams",
        "streamUrls",
        "stream_urls",
        "sources",
        "links",
        "urls"
    ]

    for key in possible_keys:
        value = item.get(key)

        if isinstance(value, list):
            for stream in value:
                normalized = normalize_stream(
                    stream,
                    default_referer
                )

                if normalized:
                    streams.append(normalized)

        elif isinstance(value, dict):
            for label, url in value.items():
                normalized = normalize_stream(
                    {
                        "label": label,
                        "url": url
                    },
                    default_referer
                )

                if normalized:
                    streams.append(normalized)

    for key in ("source_live", "source", "url", "link"):
        value = item.get(key)

        if isinstance(value, str):
            normalized = normalize_stream(
                value,
                default_referer
            )

            if normalized:
                streams.append(normalized)

    result = []
    seen = set()

    for stream in streams:
        if stream["url"] not in seen:
            seen.add(stream["url"])
            result.append(stream)

    return result


# ============================================================
# GENERIC MATCH PARSER
# ============================================================

def parse_generic_match(item, api):
    if not isinstance(item, dict):
        return None

    home = first_value(
        get_nested(item, "teams", "home", "name"),
        get_nested(item, "homeTeam", "name"),
        item.get("home_team"),
        item.get("homeTeamName"),
        item.get("team_1"),
        item.get("home"),
        "Đội nhà"
    )

    away = first_value(
        get_nested(item, "teams", "away", "name"),
        get_nested(item, "awayTeam", "name"),
        item.get("away_team"),
        item.get("awayTeamName"),
        item.get("team_2"),
        item.get("away"),
        "Đội khách"
    )

    home_logo = first_value(
        get_nested(item, "teams", "home", "logo"),
        get_nested(item, "homeTeam", "logo"),
        item.get("team_1_logo"),
        item.get("home_logo")
    )

    away_logo = first_value(
        get_nested(item, "teams", "away", "logo"),
        get_nested(item, "awayTeam", "logo"),
        item.get("team_2_logo"),
        item.get("away_logo")
    )

    league = first_value(
        get_nested(item, "league", "name"),
        item.get("competition"),
        item.get("league_name"),
        item.get("league"),
        item.get("tournament"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = first_value(
            league.get("name"),
            league.get("title"),
            "Thể thao"
        )

    match_time = first_value(
        item.get("matchTime"),
        item.get("match_time"),
        item.get("start_date"),
        item.get("startTime"),
        item.get("start_at"),
        item.get("date"),
        item.get("time")
    )

    streams = extract_streams(
        item,
        api.get("default_stream_referer", "")
    )

    blvs = []

    if streams:
        blvs.append({
            "name": api.get("display_name", api["name"]),
            "streams": streams
        })

    return {
        "externalId": clean(
            first_value(
                item.get("id"),
                item.get("match_id"),
                item.get("externalId")
            )
        ),
        "matchTime": clean(match_time),
        "status": clean(
            first_value(
                item.get("status"),
                "live" if item.get("is_live") else ""
            )
        ),
        "teams": {
            "home": {
                "name": clean(home),
                "logo": clean(home_logo)
            },
            "away": {
                "name": clean(away),
                "logo": clean(away_logo)
            }
        },
        "league": {
            "name": clean(league),
            "logo": clean(
                first_value(
                    get_nested(item, "league", "logo"),
                    item.get("league_logo")
                )
            )
        },
        "blvs": blvs
    }


# ============================================================
# GA VANG TV PARSER
# ============================================================

def fetch_colatv(api):
    payload = request_json(
        api["url"],
        headers=api.get("headers", {}),
    )

    # API có thể trả danh sách trực tiếp hoặc bọc trong data
    if isinstance(payload, list):
        items = payload
    else:
        items = unwrap_data(payload)

    results = []

    for item in items:
        if not isinstance(item, dict):
            continue

        streams = []
        seen = set()

        def add_stream(value, name="ColaTV"):
            if isinstance(value, dict):
                value = (
                    value.get("url")
                    or value.get("link")
                    or value.get("src")
                    or value.get("playStreamAddress2")
                    or value.get("playStreamAddress")
                )

            if isinstance(value, list):
                for entry in value:
                    add_stream(entry, name)
                return

            if not isinstance(value, str):
                return

            url = value.strip()

            if not url.startswith(("http://", "https://")):
                return

            if url in seen:
                return

            seen.add(url)

            stream = normalize_stream(url)
            if stream:
                if isinstance(stream, dict):
                    stream.setdefault("name", name)
                streams.append(stream)

        # 1. Luồng phát chính
        add_stream(item.get("videoUrl"), "ColaTV")

        # 2. Luồng từ danh sách BLV
        anchors = item.get("anchorAppointmentVoList") or []

        for anchor in anchors:
            if not isinstance(anchor, dict):
                continue

            anchor_name = (
                anchor.get("nickName")
                or anchor.get("houseName")
                or "ColaTV"
            )

            # Ưu tiên M3U8, sau đó đến FLV
            add_stream(
                anchor.get("playStreamAddress2"),
                anchor_name
            )

            add_stream(
                anchor.get("servers"),
                anchor_name
            )

            add_stream(
                anchor.get("playStreamAddress"),
                anchor_name
            )

        # Không có link phát hợp lệ thì bỏ trận
        if not streams:
            continue

        # Ưu tiên M3U8 trước FLV
        streams.sort(
            key=lambda s: (
                0 if ".m3u8" in (
                    s.get("url", "") if isinstance(s, dict) else str(s)
                ).lower() else 1
            )
        )

        match_id = (
            item.get("matchId")
            or item.get("match_id")
            or item.get("id")
        )

        results.append({
            "externalId": str(match_id or ""),
            "matchTime": (
                item.get("matchTime")
                or item.get("match_time")
            ),
            "status": (
                item.get("match_status")
                or item.get("matchStatus")
                or "unknown"
            ),
            "teams": {
                "home": {
                    "name": item.get("homeTeamName", ""),
                    "logo": item.get("homeTeamLogo", ""),
                },
                "away": {
                    "name": item.get("awayTeamName", ""),
                    "logo": item.get("awayTeamLogo", ""),
                },
            },
            "league": {
                "name": item.get("competitionName", ""),
                "logo": item.get("competitionLogo", ""),
            },
            "blvs": [{
                "name": "ColaTV",
                "streams": streams,
            }],
        })

    print(
        f"[ColaTV] Lấy được {len(results)} trận có luồng phát"
    )

    return results

def parse_colatv_detail(payload, api=None):
    api = api or {}

    if not isinstance(payload, dict):
        return {}, []

    # Bóc các lớp bọc thường gặp trong phản hồi API.
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data

    match = payload.get("match")
    if not isinstance(match, dict):
        match = payload

    raw = match.get("raw_data")
    if not isinstance(raw, dict):
        raw = {}

    streams = []

    def add_stream(value, label="Live"):
        """Nhận URL, object URL hoặc danh sách URL."""
        if isinstance(value, str):
            stream = normalize_stream({
                "url": value,
                "label": label
            })

            if stream:
                streams.append(stream)

        elif isinstance(value, dict):
            stream = normalize_stream(value)

            if stream:
                streams.append(stream)
            else:
                for key in (
                    "url", "link", "src", "source",
                    "playStreamAddress", "playStreamAddress2",
                    "videoUrl"
                ):
                    if value.get(key):
                        add_stream(value[key], key)

        elif isinstance(value, list):
            for index, item in enumerate(value, start=1):
                add_stream(item, f"HD{index}")

    # URL trực tiếp.
    for obj in (payload, match, raw):
        for key in (
            "videoUrl",
            "video_url",
            "playStreamAddress2",
            "playStreamAddress"
        ):
            if obj.get(key):
                add_stream(obj[key], key)

    # Danh sách máy chủ / người phát.
    for obj in (payload, match, raw):
        for key in (
            "anchorAppointmentVoList",
            "servers",
            "streams",
            "streamUrls"
        ):
            value = obj.get(key)

            if isinstance(value, list):
                for anchor in value:
                    if isinstance(anchor, dict):
                        for field in (
                            "streamUrls",
                            "videoUrl",
                            "playStreamAddress2",
                            "servers",
                            "playStreamAddress",
                            "url",
                            "link",
                            "src"
                        ):
                            if anchor.get(field):
                                add_stream(
                                    anchor[field],
                                    first_value(
                                        anchor.get("nickName"),
                                        anchor.get("name"),
                                        field
                                    )
                                )
                    else:
                        add_stream(anchor, key)

            elif isinstance(value, dict):
                add_stream(value, key)

    # Bỏ URL trùng.
    unique = []
    seen = set()

    for stream in streams:
        url = stream["url"]

        if url not in seen:
            seen.add(url)
            unique.append(stream)

    # Ưu tiên M3U8, FLV là phương án dự phòng.
    unique.sort(
        key=lambda stream: (
            0 if ".m3u8" in stream["url"].lower()
            else 1 if ".flv" in stream["url"].lower()
            else 2
        )
    )

    return match, unique

def parse_gavang_match(item, api):
    if not isinstance(item, dict):
        return None

    home = first_value(
        get_nested(item, "homeTeam", "name"),
        item.get("homeTeam"),
        item.get("home_team"),
        "Đội nhà"
    )

    away = first_value(
        get_nested(item, "awayTeam", "name"),
        item.get("awayTeam"),
        item.get("away_team"),
        "Đội khách"
    )

    streams = []

    appointments = item.get(
        "anchorAppointmentVoList",
        []
    )

    if isinstance(appointments, list):
        for appointment in appointments:
            if not isinstance(appointment, dict):
                continue

            urls = appointment.get("streamUrls", [])

            if isinstance(urls, list):
                for stream in urls:
                    normalized = normalize_stream(
                        stream,
                        api.get("default_stream_referer", "")
                    )

                    if normalized:
                        streams.append(normalized)

    if not streams:
        streams = extract_streams(
            item,
            api.get("default_stream_referer", "")
        )

    league = first_value(
        get_nested(item, "competition", "name"),
        item.get("competition"),
        item.get("league"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = first_value(
            league.get("name"),
            league.get("title"),
            "Thể thao"
        )

    return {
        "externalId": clean(item.get("id")),
        "matchTime": first_value(
            item.get("startTime"),
            item.get("matchTime"),
            item.get("match_time"),
            item.get("start_date"),
            item.get("start_at"),
            item.get("matchDate"),
            item.get("match_date"),
            item.get("date"),
            item.get("timestamp"),
            item.get("time")
        ),
        "status": clean(item.get("status")),
        "teams": {
            "home": {
                "name": clean(home),
                "logo": clean(
                    get_nested(item, "homeTeam", "logo")
                )
            },
            "away": {
                "name": clean(away),
                "logo": clean(
                    get_nested(item, "awayTeam", "logo")
                )
            }
        },
        "league": {
            "name": clean(league),
            "logo": ""
        },
        "blvs": [{
            "name": api.get("display_name", "Gà Vàng TV"),
            "streams": streams
        }] if streams else []
    }


# ============================================================
# PLAPI
# ============================================================

def plapi_get_live(api, match_id):
    endpoint = api.get(
        "match_live_url",
        api.get(
            "live_endpoint",
            "https://api.plapi202624081158.com/match/{id}/live"
        )
    )

    url = endpoint.replace(
        "{id}",
        urllib.parse.quote(clean(match_id), safe="")
    )

    try:
        payload = request_json(
            url,
            method=api.get("live_method", "GET"),
            headers=api.get("headers", {})
        )

        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]

        if isinstance(payload, list):
            payload = payload[0] if payload else {}

        if not isinstance(payload, dict):
            return []

        priority = api.get(
            "stream_priority",
            ["hd_1", "hd_2", "sd_1", "sd_2", "source"]
        )

        labels = {
            "hd_1": "HD1",
            "hd_2": "HD2",
            "sd_1": "SD1",
            "sd_2": "SD2",
            "source": "Live"
        }

        streams = []
        seen = set()

        for key in priority:
            value = payload.get(key)

            if isinstance(value, dict):
                value = first_value(
                    value.get("url"),
                    value.get("source"),
                    value.get("link")
                )

            elif isinstance(value, list):
                value = next(
                    (
                        first_value(
                            x.get("url"),
                            x.get("source"),
                            x.get("link")
                        )
                        for x in value
                        if isinstance(x, dict)
                    ),
                    None
                )

            if not isinstance(value, str):
                continue

            value = value.strip()

            if not value.startswith(("http://", "https://")):
                continue

            if value in seen:
                continue

            stream = normalize_stream(
                {
                    "url": value,
                    "label": labels.get(key, key.upper())
                },
                api.get("default_stream_referer", "")
            )

            if stream:
                streams.append(stream)
                seen.add(value)

        return streams

    except Exception as exc:
        log.warning(
            "PLAPI live resolver lỗi cho trận %s: %s",
            match_id,
            exc
        )
        return []

def plapi_is_finished(item):
    if not isinstance(item, dict):
        return False

    # Chỉ chấp nhận cờ kết thúc rõ ràng.
    for key in ("is_finished", "finished", "is_ended", "ended"):
        value = item.get(key)

        if value is True:
            return True

        if str(value).strip().lower() in (
            "1", "true", "yes"
        ):
            return True

    # Chỉ nhận diện những trạng thái kết thúc tường minh.
    for key in ("status", "match_status", "state"):
        value = str(item.get(key, "")).strip().lower()

        if value in (
            "finished",
            "ended",
            "completed",
            "complete",
            "full_time",
            "full-time",
            "ft"
        ):
            return True

    return False

def parse_plapi_match(item, api):
    if not isinstance(item, dict):
        return None

    if plapi_is_finished(item):
        return None

    match_id = clean(item.get("id"))

    home = first_value(
        item.get("team_1"),
        "Đội nhà"
    )

    away = first_value(
        item.get("team_2"),
        "Đội khách"
    )

    league = first_value(
        item.get("league"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = first_value(
            league.get("name"),
            league.get("title"),
            "Thể thao"
        )

    live_value = item.get("is_live")

    is_live = (
        live_value is True
        or str(live_value).strip().lower() in ("1", "true", "yes")
    )

    streams = []

    # source_live là URL do API cung cấp.
    source_live = item.get("source_live")

    if source_live:
        stream = normalize_stream(
            {
                "url": source_live,
                "label": "Live"
            },
            api.get("default_stream_referer", "")
        )

        if stream:
            streams.append(stream)

    # Chỉ gọi resolver đối với trận đang live.
    if is_live and match_id:
        resolved = plapi_get_live(api, match_id)

        # Đưa link resolver lên trước nếu có.
        if resolved:
            streams = resolved + streams

    unique = []
    seen = set()

    for stream in streams:
        if stream["url"] not in seen:
            seen.add(stream["url"])
            unique.append(stream)

    # Không dùng stream_key để tạo URL.
    return {
        "externalId": match_id,
        "matchTime": clean(item.get("start_date")),
        "status": "live" if is_live else "",
        "teams": {
            "home": {
                "name": clean(home),
                "logo": clean(item.get("team_1_logo"))
            },
            "away": {
                "name": clean(away),
                "logo": clean(item.get("team_2_logo"))
            }
        },
        "league": {
            "name": clean(league),
            "logo": ""
        },
        "blvs": [{
            "name": api.get("display_name", "PLAPI"),
            "streams": unique
        }] if unique else []
    }


def fetch_plapi(api):
    url = api["url"]
    headers = api.get("headers", {})
    body_template = api.get("body", {})
    pagination = api.get("pagination", {})

    page_key = pagination.get(
        "page_param",
        pagination.get("page_key", "page")
    )
    limit_key = pagination.get(
        "limit_param",
        pagination.get("limit_key", "limit")
    )

    start_page = int(pagination.get("start_page", 1))
    page_limit = int(
        pagination.get(
            "limit",
            pagination.get(
                "page_limit",
                body_template.get(limit_key, 100)
            )
        )
    )
    max_pages = int(pagination.get("max_pages", 100))

    pagination_enabled = pagination.get("enabled", True)

    all_items = []
    seen_ids = set()

    pages_to_fetch = max_pages if pagination_enabled else 1

    for page in range(start_page, start_page + pages_to_fetch):
        body = dict(body_template)
        body[page_key] = page
        body[limit_key] = page_limit

        payload = request_json(
            url,
            method=api.get("method", "POST"),
            headers=headers,
            body=body
        )

        items = unwrap_data(payload)

        if not items:
            log.info("PLAPI: hết dữ liệu tại trang %s", page)
            break

        added = 0

        for item in items:
            if not isinstance(item, dict):
                continue

            item_id = clean(item.get("id"))

            if item_id and item_id in seen_ids:
                continue

            if item_id:
                seen_ids.add(item_id)

            all_items.append(item)
            added += 1

        log.info(
            "PLAPI: trang %s nhận %s bản ghi, tổng %s",
            page,
            added,
            len(all_items)
        )

        if len(items) < page_limit:
            break

        if added == 0:
            break

    live_count = sum(
        1 for item in all_items
        if str(item.get("is_live", "")).strip().lower()
        in ("true", "1", "yes")
    )

    log.info(
        "PLAPI: tổng %s bản ghi, trong đó %s trận đang live",
        len(all_items),
        live_count
    )

    return all_items

# ============================================================
# API FETCH AND PARSING
# ============================================================

# ============================================================
# SPORT DETECTION (x-sport) - cùng quy tắc với app MHD Sports
# ============================================================

import unicodedata

_SPORT_BASKETBALL = re.compile(
    r"\b(nba|wnba|g league|nba2k|ncaab|ncaa basketball|vba|fiba|nbl|cba|kbl|pba|bsn|acb|liga acb|bcl|eurocup|"
    r"eurobasket|euroleague|basketball|basket|bong ro|b ?league|lega basket|vtb united|"
    r"atlanta hawks|boston celtics|brooklyn nets|charlotte hornets|chicago bulls|cleveland cavaliers|dallas mavericks|"
    r"denver nuggets|detroit pistons|golden state warriors|houston rockets|indiana pacers|la clippers|los angeles clippers|"
    r"los angeles lakers|la lakers|memphis grizzlies|miami heat|milwaukee bucks|minnesota timberwolves|new orleans pelicans|"
    r"new york knicks|oklahoma city thunder|okc thunder|orlando magic|philadelphia 76ers|phoenix suns|portland trail blazers|"
    r"sacramento kings|san antonio spurs|toronto raptors|utah jazz|washington wizards|"
    r"las vegas aces|new york liberty|seattle storm|chicago sky|connecticut sun|atlanta dream|indiana fever|minnesota lynx|"
    r"phoenix mercury|washington mystics|dallas wings|los angeles sparks|golden state valkyries|"
    r"lakers|celtics|cavaliers|mavericks|nuggets|pistons|clippers|grizzlies|timberwolves|pelicans|knicks|76ers|sixers|"
    r"trail blazers|raptors|pacers|bucks)\b"
)

_SPORT_RULES = [
    ("BASKETBALL", _SPORT_BASKETBALL),
    ("VOLLEYBALL", re.compile(r"\b(volleyball|volley|bong chuyen|vnl|fivb)\b")),
    ("TENNIS", re.compile(r"\b(tennis|atp|wta|wimbledon|roland garros|davis cup|billie jean king|grand slam|us open|australian open|itf)\b")),
    ("BADMINTON", re.compile(r"\b(badminton|cau long|bwf|all england|thomas cup|uber cup|sudirman)\b")),
    ("BILLIARDS", re.compile(r"\b(billiards?|bida|bi[ -]a|snooker|carom|three cushion|3 cushion|nine ball|eight ball|ten ball|[89] ?-?ball|10 ?-?ball|pool billiards?|wpa|uk pool)\b")),
    ("ESPORTS", re.compile(r"\b(esports?|e ?-?sports?|league of legends|lol|lck|lec|lcs|vcs|msi|dota ?2?|cs ?go|cs ?2|counter ?-?strike|valorant|vct|pubg|pmgc|mobile legends|mlbb|lien quan|aov|arena of valor|honor of kings|free fire|fortnite|overwatch|rainbow six|apex legends|starcraft|rocket league|tft|pgl|esl|iem|fifa online|ea fc online|fc online)\b")),
    ("FOOTBALL", re.compile(
        r"\b(football|soccer|bong da|premier league|epl|la ?liga|serie a|bundesliga|ligue 1|champions league|"
        r"europa league|conference league|uefa|fifa|world cup|v ?league|afc|copa|mls|j ?league|k ?league|"
        r"wk ?league|w ?league|fa cup|carabao|efl|nations league|ngoai hang|giao huu|friendly|"
        r"cup of nations|afcon|futsal|fc|u ?\d{2})\b"
    )),
]

# Môn không có trong bộ lọc của app: không đoán bừa là bóng đá.
_SPORT_OTHER = re.compile(
    r"\b(cricket|ipl|t20|odi|bbl|psl|rugby|baseball|mlb|nfl|nhl|hockey|mma|ufc|boxing|golf|nascar|f1|formula 1|motogp|handball|table tennis|bong ban|darts|cycling|swimming)\b"
)


def _fold_text(value):
    text = unicodedata.normalize("NFD", clean(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("đ", "d").replace("Đ", "D").lower()


def detect_sport(*texts):
    """Trả về FOOTBALL/BASKETBALL/VOLLEYBALL/TENNIS/BADMINTON/BILLIARDS/ESPORTS, hoặc '' nếu không chắc."""
    joined = _fold_text(" | ".join(t for t in texts if t))

    if not joined.strip():
        return ""

    for name, pattern in _SPORT_RULES:
        if pattern.search(joined):
            return name

    return ""


def match_sport(match):
    """Môn của trận: ưu tiên trường do API trả về, sau đó mới đoán theo giải/đội."""
    declared = clean(match.get("sport"))
    found = detect_sport(declared) if declared else ""

    if found:
        return found

    teams = match.get("teams", {})

    found = detect_sport(
        match.get("league", {}).get("name"),
        teams.get("home", {}).get("name"),
        teams.get("away", {}).get("name")
    )

    if found:
        return found

    # Không nhận ra môn nào và cũng không phải môn ngoài danh sách => nguồn của dự án chủ yếu là bóng đá.
    joined = _fold_text(" ".join([
        match.get("league", {}).get("name", ""),
        teams.get("home", {}).get("name", ""),
        teams.get("away", {}).get("name", "")
    ]))

    return "" if _SPORT_OTHER.search(joined) else "FOOTBALL"


# ============================================================
# CHUOICHIENTV
# ============================================================
# Mỗi trận có các nhóm bình luận viên (blvs, blvs_bonglau, blvs_nguoitho, ...). Mỗi BLV có
# các luồng HD1/HD2/FHD. Trước đây Chuối Chiên đi qua parser chung (không đọc các nhóm này)
# nên ra 0 trận; parser riêng dưới đây đọc đúng cấu trúc đó và chịu được vài kiểu đặt tên khoá.

_QUALITY_KEYS = [
    ("hd1", "HD1"), ("hd_1", "HD1"), ("hd2", "HD2"), ("hd_2", "HD2"),
    ("fhd", "FHD"), ("fhd1", "FHD"), ("fhd_1", "FHD"), ("fhd2", "FHD2"),
    ("sd", "SD"), ("sd1", "SD1"), ("sd_1", "SD1"), ("sd2", "SD2"), ("sd_2", "SD2"),
    ("flv", "FLV"), ("hls", "HLS"), ("m3u8", "HLS"),
    ("stream", "Live"), ("stream_url", "Live"), ("streamUrl", "Live"),
    ("url", "Live"), ("link", "Live"), ("source", "Live")
]


def _name_of(value):
    if isinstance(value, dict):
        return clean(first_value(
            value.get("name"), value.get("title"), value.get("short_name"), value.get("shortName")
        ))
    return clean(value)


def _logo_of(value):
    if isinstance(value, dict):
        return clean(first_value(
            value.get("logo"), value.get("image"), value.get("icon"), value.get("avatar")
        ))
    return ""


def _ccv_streams(blv, default_referer):
    streams = []

    # 1) danh sách / dict luồng: streams, links, sources, ...
    streams.extend(extract_streams(blv, default_referer))

    # 2) khoá chất lượng nằm thẳng trong đối tượng BLV: hd1, hd2, fhd, ...
    for key, label in _QUALITY_KEYS:
        value = blv.get(key)

        if isinstance(value, dict):
            value = first_value(value.get("url"), value.get("link"), value.get("source"))

        if not isinstance(value, str):
            continue

        stream = normalize_stream({"url": value, "label": label}, default_referer)

        if stream:
            streams.append(stream)

    # 3) tên luồng rỗng/chung chung => đặt HD1, HD2, ... theo thứ tự
    unique = []
    seen = set()

    for stream in streams:
        if stream["url"] in seen:
            continue

        seen.add(stream["url"])
        unique.append(stream)

    generic = {"", "live", "link", "url", "source", "stream"}

    for index, stream in enumerate(unique, start=1):
        if clean(stream.get("label")).lower() in generic and len(unique) > 1:
            stream["label"] = "HD%d" % index

    return unique


def parse_chuoichien_match(item, api):
    if not isinstance(item, dict):
        return None

    # Một số phản hồi bọc trận trong "match"/"data".
    for wrapper in ("match", "data"):
        inner = item.get(wrapper)

        if isinstance(inner, dict) and (
            inner.get("homeTeam") or inner.get("home") or inner.get("home_team")
            or any(k.startswith("blvs") for k in inner)
        ):
            merged = dict(inner)
            for key, value in item.items():
                merged.setdefault(key, value)
            item = merged
            break

    home_raw = first_value(
        item.get("homeTeam"), item.get("home_team"), item.get("home"),
        get_nested(item, "teams", "home"), item.get("team_1"), item.get("homeTeamName")
    )
    away_raw = first_value(
        item.get("awayTeam"), item.get("away_team"), item.get("away"),
        get_nested(item, "teams", "away"), item.get("team_2"), item.get("awayTeamName")
    )

    home = _name_of(home_raw) or "Đội nhà"
    away = _name_of(away_raw) or "Đội khách"

    league_raw = first_value(
        item.get("league"), item.get("competition"), item.get("tournament"),
        item.get("league_name"), item.get("leagueName")
    )
    league = _name_of(league_raw) or "Thể thao"

    default_referer = api.get("default_stream_referer", "")
    blvs = []
    seen_groups = set()

    group_keys = list(INCLUDE_GROUPS) + [
        key for key in item if key.startswith("blvs") and key not in INCLUDE_GROUPS
    ]

    for key in group_keys:
        group = item.get(key)

        if key in seen_groups or not isinstance(group, list):
            continue

        seen_groups.add(key)

        for blv in group:
            if not isinstance(blv, dict):
                continue

            streams = _ccv_streams(blv, default_referer)

            if not streams:
                continue

            blvs.append({
                "name": clean(first_value(
                    blv.get("name"), blv.get("blv_name"), blv.get("blvName"), blv.get("nickname"),
                    blv.get("display_name"), blv.get("displayName"), blv.get("username"),
                    blv.get("title"), api.get("display_name"), "BLV"
                )),
                "streams": streams
            })

    # Luồng gắn thẳng vào trận (không qua BLV).
    direct = extract_streams(item, default_referer)

    if direct and not blvs:
        blvs.append({
            "name": api.get("display_name", api["name"]),
            "streams": direct
        })

    status = clean(first_value(
        item.get("status"), item.get("match_status"),
        "live" if item.get("is_live") or item.get("isLive") else ""
    ))

    return {
        "externalId": clean(first_value(item.get("id"), item.get("match_id"), item.get("matchId"))),
        "matchTime": clean(first_value(
            item.get("matchTime"), item.get("match_time"), item.get("startTime"),
            item.get("start_time"), item.get("start_date"), item.get("startAt"),
            item.get("date"), item.get("time")
        )),
        "status": status,
        "sport": clean(first_value(
            item.get("sport"), item.get("sport_type"), item.get("sportType"),
            get_nested(item, "sport", "name") if isinstance(item.get("sport"), dict) else None
        )),
        "teams": {
            "home": {"name": home, "logo": _logo_of(home_raw) or clean(first_value(item.get("home_logo"), item.get("homeLogo"), item.get("team_1_logo")))},
            "away": {"name": away, "logo": _logo_of(away_raw) or clean(first_value(item.get("away_logo"), item.get("awayLogo"), item.get("team_2_logo")))}
        },
        "league": {
            "name": league,
            "logo": _logo_of(league_raw) or clean(first_value(item.get("league_logo"), item.get("leagueLogo")))
        },
        "blvs": blvs
    }


def fetch_chuoichien(api):
    """Gọi API với từng bộ tham số trong params_variants (vd type=live và type=blv), gộp kết quả.
    Một biến thể lỗi không làm hỏng cả nguồn nếu biến thể khác vẫn có dữ liệu."""
    variants = api.get("params_variants") or [api.get("params") or {}]

    items = []
    last_error = None
    ok = 0

    for params in variants:
        try:
            payload = request_json(
                api["url"],
                method=api.get("method", "GET"),
                headers=api.get("headers", {}),
                params=params,
                body=api.get("body")
            )
            chunk = unwrap_data(payload)
            ok += 1
            log.info("%s: params=%s -> %s bản ghi", api.get("name", "API"), json.dumps(params, ensure_ascii=False), len(chunk))
            items.extend(chunk)

        except Exception as exc:
            last_error = exc
            log.warning("%s: params=%s lỗi: %s", api.get("name", "API"), params, exc)

    if ok == 0 and last_error is not None:
        raise last_error

    return items


def merge_matches_by_id(matches):
    """Gộp các bản ghi trùng id (từ nhiều biến thể params): hợp nhất BLV, bỏ luồng trùng URL."""
    merged = {}
    order = []

    for match in matches:
        key = clean(match.get("externalId")) or match_tvg_id(match)

        if key not in merged:
            merged[key] = match
            order.append(key)
            continue

        base = merged[key]
        known = {
            stream["url"]
            for blv in base["blvs"]
            for stream in blv["streams"]
        }

        for blv in match["blvs"]:
            fresh = [s for s in blv["streams"] if s["url"] not in known]

            if not fresh:
                continue

            known.update(s["url"] for s in fresh)
            target = next((b for b in base["blvs"] if b["name"] == blv["name"]), None)

            if target:
                target["streams"].extend(fresh)
            else:
                base["blvs"].append({"name": blv["name"], "streams": fresh})

        for field in ("status", "sport"):
            if not base.get(field) and match.get(field):
                base[field] = match[field]

    return [merged[key] for key in order]


# ============================================================
# REFERER THEO NGUỒN (Phá Làng TV, ...)
# ============================================================

def _origin_of(url):
    parsed = urllib.parse.urlparse(url)

    if not parsed.scheme or not parsed.netloc:
        return ""

    return f"{parsed.scheme}://{parsed.netloc}/"


def attach_api_referers(matches, api):
    """Gắn Referer cho luồng chưa có Referer, theo thứ tự ưu tiên:
    1. referer_by_host[host]  (khai báo theo máy chủ phát)
    2. stream_referer         (một Referer chung cho cả nguồn, thường là trang player của nhà đài)
    3. referer_self_origin    (dùng chính origin của máy chủ phát: https://<host>/)
    Referer do API tự cung cấp trong stream.headers luôn được giữ nguyên."""
    by_host = {
        clean(host).lower(): clean(ref)
        for host, ref in (api.get("referer_by_host") or {}).items()
        if clean(host) and clean(ref)
    }
    common = clean(api.get("stream_referer"))
    self_origin = bool(api.get("referer_self_origin"))

    if not (by_host or common or self_origin):
        return

    for match in matches:
        for blv in match.get("blvs", []):
            for stream in blv.get("streams", []):
                headers = stream.setdefault("headers", {})

                if any(k.lower() == "referer" for k in headers):
                    continue

                host = (urllib.parse.urlparse(stream["url"]).hostname or "").lower()
                referer = by_host.get(host) or common or (_origin_of(stream["url"]) if self_origin else "")

                if referer:
                    headers["Referer"] = referer



def fetch_api(api):

    if api.get("parser") == "colatv" or api.get("id") == "colatv":
        return fetch_colatv(api)

    parser = clean(
        api.get("parser") or api.get("api_type")
    ).lower()

    if parser in ("plapi", "plapi_graph"):
        return fetch_plapi(api)

    if parser == "chuoichientv" or api.get("id") == "chuoichientv":
        return fetch_chuoichien(api)

    payload = request_json(
        api["url"],
        method=api.get("method", "GET"),
        headers=api.get("headers", {}),
        params=api.get("params"),
        body=api.get("body")
    )

    return unwrap_data(payload)


def parse_api_items(items, api):
    parser = clean(
        api.get("parser") or api.get("api_type")
    ).lower()

    result = []

    # ColaTV đã được chuẩn hóa trong fetch_colatv()
    # Không đưa qua parse_generic_match() lần thứ hai.
    if parser == "colatv" or api.get("id") == "colatv":
        result = [
            item for item in items
            if isinstance(item, dict)
            and isinstance(item.get("teams"), dict)
            and isinstance(item.get("blvs"), list)
        ]

        attach_api_referers(result, api)
        return result

    for item in items:
        try:
            if parser in ("plapi", "plapi_graph"):
                match = parse_plapi_match(item, api)

            elif (
                parser == "chuoichientv"
                or api.get("id") == "chuoichientv"
            ):
                match = parse_chuoichien_match(item, api)

            elif (
                parser == "gavangtv"
                or api.get("id") == "gavangtv"
            ):
                match = parse_gavang_match(item, api)

            else:
                match = parse_generic_match(item, api)

            if match:
                result.append(match)

        except Exception as exc:
            log.warning(
                "%s: bỏ qua bản ghi lỗi: %s",
                api.get("name", "API"),
                exc
            )

    if parser == "chuoichientv" or api.get("id") == "chuoichientv":
        result = merge_matches_by_id(result)

        if items and not any(m["blvs"] for m in result):
            sample = next((i for i in items if isinstance(i, dict)), {})
            log.warning(
                "%s: có %s bản ghi nhưng không đọc được luồng nào. Các khoá của bản ghi đầu: %s",
                api.get("name", "API"),
                len(items),
                ", ".join(sorted(sample.keys()))
            )

    attach_api_referers(result, api)

    return result


# ============================================================
# REFERER CACHE AND STREAM HEADERS
# ============================================================

def load_referer_cache():
    if not CACHE_FILE.exists():
        return {}

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


REFERER_CACHE = load_referer_cache()


def save_referer_cache():
    atomic_write_json(
        CACHE_FILE,
        REFERER_CACHE
    )


def apply_stream_headers(stream):
    url = stream["url"]
    headers = dict(stream.get("headers") or {})

    host = urllib.parse.urlparse(url).hostname or ""

    for rule in CONFIG.get("stream_header_rules", []):
        rule_host = clean(rule.get("host")).lower()

        if rule_host and (
            host.lower() == rule_host
            or host.lower().endswith("." + rule_host)
        ):
            headers.update(rule.get("headers", {}))

    cached = REFERER_CACHE.get(host)

    if cached and not any(
        key.lower() == "referer" for key in headers
    ):
        headers["Referer"] = cached

    return headers


# ============================================================
# M3U GENERATOR
# ============================================================

def m3u_escape(value):
    return clean(value).replace('"', "'").replace("\n", " ")


def match_tvg_id(match):
    """tvg-id ổn định cho mỗi trận; mọi luồng của trận dùng chung ID này."""
    external_id = clean(match.get("externalId"))

    if external_id:
        return f"mhd-match-{external_id}"

    teams = match.get("teams", {})
    raw = "|".join([
        clean(teams.get("home", {}).get("name")),
        clean(teams.get("away", {}).get("name")),
        clean(match.get("matchTime"))
    ])

    return "mhd-match-" + hashlib.md5(
        raw.encode("utf-8")
    ).hexdigest()[:12]


def match_vs_title(match):
    teams = match["teams"]

    return "{} vs {}".format(
        teams["home"]["name"],
        teams["away"]["name"]
    )


def match_time_label(match):
    dt = parse_datetime(match.get("matchTime"))

    if not dt:
        return ""

    return dt.astimezone(VN_TZ).strftime("%d/%m %H:%M")


def make_match_title(match, broadcaster, stream):
    parts = [match_vs_title(match)]

    for value in (
        match_time_label(match),
        clean(broadcaster),
        clean(stream.get("label"))
    ):
        if value:
            parts.append(value)

    return " | ".join(parts)


def stream_extinf(match, broadcaster, stream, api_logo=""):
    source = match.get("_source", {})

    league = match["league"]["name"]
    teams = match["teams"]

    title = make_match_title(match, broadcaster, stream)

    tvg_name = " - ".join(
        part for part in (
            match_vs_title(match),
            clean(broadcaster),
            clean(stream.get("label"))
        ) if part
    )

    # Logo giải đấu trước, sau đó mới tới logo đội nhà/đội khách.
    logo = first_value(
        match["league"].get("logo"),
        teams["home"].get("logo"),
        teams["away"].get("logo"),
        source.get("logo"),
        api_logo
    )

    attributes = [
        'tvg-id="{}"'.format(m3u_escape(match_tvg_id(match))),
        'tvg-name="{}"'.format(m3u_escape(tvg_name)),
        'tvg-logo="{}"'.format(m3u_escape(logo)),
        # Gom mọi trận của một nhà đài vào đúng 1 nhóm, tên nhóm = tên nhà đài (vd "Chuối Chiên TV").
        # Chỉ khi nguồn không có tên mới quay về tên giải đấu.
        'group-title="{}"'.format(m3u_escape(source.get("name") or league)),
        'x-source="{}"'.format(
            m3u_escape(source.get("id") or broadcaster)
        )
    ]

    sport = match_sport(match)

    if sport:
        attributes.append('x-sport="{}"'.format(sport))

    # Logo hiển thị bên cạnh tiêu đề nhóm (app đọc thuộc tính group-logo); lấy từ "logo" của nguồn trong config.json.
    if source.get("logo"):
        attributes.append('group-logo="{}"'.format(m3u_escape(source["logo"])))

    return (
        "#EXTINF:-1 "
        + " ".join(attributes)
        + ","
        + m3u_escape(title)
    )


def stream_header_lines(stream):
    headers = apply_stream_headers(stream)

    lines = []

    for key, value in headers.items():
        key = clean(key).lower()
        value = clean(value)

        if not key or not value:
            continue

        if key == "referer":
            lines.append(
                "#EXTVLCOPT:http-referrer=" + value
            )

        elif key == "user-agent":
            lines.append(
                "#EXTVLCOPT:http-user-agent=" + value
            )

        else:
            lines.append(
                "#EXTHTTP:{"
                + json.dumps(
                    {key: value},
                    ensure_ascii=False
                )
                + "}"
            )

    return lines


def collect_playlist_entries(matches):
    entries = []

    for match in matches:
        for broadcaster in match.get("blvs", []):
            broadcaster_name = clean(
                broadcaster.get("name")
            )

            for stream in broadcaster.get("streams", []):
                normalized = normalize_stream(stream)

                if not normalized:
                    continue

                entries.append({
                    "match": match,
                    "broadcaster": broadcaster_name,
                    "stream": normalized
                })

    return entries


def generate_m3u(entries, source_logo=""):
    lines = ["#EXTM3U"]

    seen = set()

    for entry in entries:
        match = entry["match"]
        broadcaster = entry["broadcaster"]
        stream = entry["stream"]

        url = stream["url"]

        if url in seen:
            continue

        seen.add(url)

        lines.append(
            stream_extinf(
                match,
                broadcaster,
                stream,
                source_logo
            )
        )

        lines.extend(stream_header_lines(stream))
        lines.append(url)

    return "\n".join(lines) + "\n"


# ============================================================
# EPG GENERATOR
# ============================================================

def epg_time(value):
    dt = parse_datetime(value)

    if not dt:
        return ""

    return dt.strftime("%Y%m%d%H%M%S +0000")


def generate_epg(entries):
    source_names = []

    for entry in entries:
        name = clean(
            entry["match"].get("_source", {}).get("name")
        )

        if name and name not in source_names:
            source_names.append(name)

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "MHD Sports",
            "source-info-name": " / ".join(source_names)
        }
    )

    seen_matches = set()

    for entry in entries:
        match = entry["match"]
        tvg_id = match_tvg_id(match)

        # Mỗi trận chỉ một channel/programme; HD1/HD2/FHD dùng chung ID.
        if tvg_id in seen_matches:
            continue

        seen_matches.add(tvg_id)

        dt = parse_datetime(match.get("matchTime"))

        if not dt:
            continue

        teams = match["teams"]
        league = match["league"]

        title = match_vs_title(match)

        channel = ET.SubElement(
            root,
            "channel",
            {"id": tvg_id}
        )

        ET.SubElement(
            channel,
            "display-name",
            {"lang": "vi"}
        ).text = title

        channel_logo = first_value(
            league.get("logo"),
            teams["home"].get("logo"),
            teams["away"].get("logo")
        )

        if channel_logo:
            ET.SubElement(channel, "icon", {"src": channel_logo})

        attrs = {
            "start": dt.astimezone(VN_TZ).strftime(
                "%Y%m%d%H%M%S +0700"
            ),
            "channel": tvg_id
        }

        # Chỉ ghi stop nếu API thật sự cung cấp giờ kết thúc.
        end_dt = parse_datetime(match.get("endTime"))

        if end_dt:
            attrs["stop"] = end_dt.astimezone(VN_TZ).strftime(
                "%Y%m%d%H%M%S +0700"
            )

        programme = ET.SubElement(root, "programme", attrs)

        ET.SubElement(
            programme,
            "title",
            {"lang": "vi"}
        ).text = title

        if clean(league.get("name")):
            ET.SubElement(
                programme,
                "sub-title",
                {"lang": "vi"}
            ).text = clean(league["name"])

        ET.SubElement(
            programme,
            "category",
            {"lang": "vi"}
        ).text = "Bóng đá"

        desc = []

        if clean(league.get("name")):
            desc.append("Giải đấu: " + clean(league["name"]))

        if clean(match.get("status")):
            desc.append("Trạng thái: " + clean(match["status"]))

        if clean(match.get("score")):
            desc.append("Tỉ số: " + clean(match["score"]))

        if desc:
            ET.SubElement(
                programme,
                "desc",
                {"lang": "vi"}
            ).text = " | ".join(desc)

        for icon in (
            teams["home"].get("logo"),
            teams["away"].get("logo"),
            league.get("logo")
        ):
            if clean(icon):
                ET.SubElement(
                    programme,
                    "icon",
                    {"src": clean(icon)}
                )

    return ET.tostring(
        root,
        encoding="unicode",
        xml_declaration=True
    )


def split_m3u_blocks(text):
    """Tách playlist thành các block: (tvg-id, url, [dòng...])."""
    blocks = []
    current = []

    for line in text.splitlines():
        line = line.rstrip()

        if not line or line.startswith("#EXTM3U"):
            continue

        current.append(line)

        if not line.startswith("#"):
            joined = "\n".join(current)
            match = re.search(r'tvg-id="([^"]*)"', joined)

            blocks.append((
                match.group(1) if match else "",
                line.strip(),
                current
            ))
            current = []

    return blocks


def build_global_outputs(apis, api_results):
    """Playlist tổng + EPG, kể cả khi một vài API lỗi.

    API thành công: dùng dữ liệu mới. API lỗi: giữ nguyên các luồng
    và chương trình EPG đã ghi lần trước của nguồn đó.
    """
    fresh_entries = []
    stale_blocks = []

    for api in apis:
        source_id = clean(api.get("id"))
        matches = api_results.get(source_id)

        if matches is not None:
            fresh_entries.extend(collect_playlist_entries(matches))
            continue

        playlist = ROOT / api.get(
            "playlist_file",
            f"thethao-{source_id}.m3u"
        )

        if playlist.exists():
            try:
                stale_blocks.extend(
                    split_m3u_blocks(
                        playlist.read_text(encoding="utf-8")
                    )
                )
            except Exception as exc:
                log.warning(
                    "%s: không đọc được playlist cũ: %s",
                    playlist.name, exc
                )

    lines = generate_m3u(fresh_entries).splitlines()

    seen_urls = {
        line.strip() for line in lines if not line.startswith("#")
    }

    stale_ids = set()
    stale_count = 0

    for tvg_id, url, block in stale_blocks:
        if url in seen_urls:
            continue

        seen_urls.add(url)
        lines.extend(block)
        stale_count += 1

        if tvg_id:
            stale_ids.add(tvg_id)

    epg = merge_stale_epg(generate_epg(fresh_entries), stale_ids)

    return (
        "\n".join(lines) + "\n",
        epg,
        len(fresh_entries),
        stale_count
    )


def merge_stale_epg(epg_text, stale_ids):
    """Chép lại channel/programme cũ của các trận thuộc API lỗi."""
    if not stale_ids or not EPG_FILE.exists():
        return epg_text

    try:
        root = ET.fromstring(epg_text.split("?>", 1)[-1])
        old_root = ET.parse(EPG_FILE).getroot()
    except Exception as exc:
        log.warning("Không gộp được EPG cũ: %s", exc)
        return epg_text

    present = {c.get("id") for c in root.findall("channel")}

    for tag in ("channel", "programme"):
        for element in old_root.findall(tag):
            key = (
                element.get("id") if tag == "channel"
                else element.get("channel")
            )

            if key in stale_ids and key not in present:
                root.append(element)

    return ET.tostring(
        root,
        encoding="unicode",
        xml_declaration=True
    )


# ============================================================
# SOURCES.JSON
# ============================================================

def load_previous_sources():
    if not SOURCES_FILE.exists():
        return {}

    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def build_source_leagues(matches):
    """Gom các trận CÓ luồng thành leagues[] -> matches[] cho ứng dụng.

    Trận sắp xếp theo giờ; giải đấu giữ thứ tự xuất hiện đầu tiên.
    """
    rows = []

    for match in matches:
        stream_count = len(collect_playlist_entries([match]))

        if stream_count == 0:
            continue

        dt = parse_datetime(match.get("matchTime"))
        local = dt.astimezone(VN_TZ) if dt else None

        external_id = clean(match.get("externalId"))
        teams = match.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        league = match.get("league", {})

        home_name = clean(home.get("name"))
        away_name = clean(away.get("name"))

        rows.append((
            local or datetime.max.replace(tzinfo=VN_TZ),
            clean(league.get("name")),
            clean(league.get("logo")),
            {
                "id": (
                    f"mhd-match-{external_id}"
                    if external_id else ""
                ),
                "title": f"{home_name} vs {away_name}".strip(),
                "home": home_name,
                "away": away_name,
                "home_logo": clean(home.get("logo")),
                "away_logo": clean(away.get("logo")),
                "time": local.isoformat() if local else "",
                "time_label": (
                    local.strftime("%d/%m %H:%M") if local else ""
                ),
                "status": clean(match.get("status")) or "scheduled",
                "score": clean(match.get("score")),
                "stream_count": stream_count
            }
        ))

    rows.sort(key=lambda row: row[0])

    leagues = {}

    for _, league_name, league_logo, item in rows:
        group = leagues.setdefault(
            league_name,
            {"name": league_name, "logo": league_logo, "matches": []}
        )

        if not group["logo"] and league_logo:
            group["logo"] = league_logo

        group["matches"].append(item)

    return list(leagues.values())


def build_sources(api_results, previous, errors=None):
    errors = errors or {}

    sources = {
        "updated_at": iso_now(),
        "sources": []
    }

    old_by_id = {
        clean(source.get("id")): source
        for source in previous.get("sources", [])
        if isinstance(source, dict)
    }

    for api in CONFIG.get("apis", []):
        source_id = clean(api.get("id"))
        current = api_results.get(source_id)

        display_name = clean(
            api.get("display_name") or api.get("name") or source_id
        )

        base = {
            "id": source_id,
            "name": clean(api.get("name") or display_name),
            "display_name": display_name,
            "initial": display_name[:1].upper(),
            "logo": api.get("logo", ""),
            "playlist": api.get(
                "playlist_file",
                f"thethao-{source_id}.m3u"
            )
        }

        # API lỗi: giữ dữ liệu lần trước, đánh dấu stale.
        if current is None:
            old = old_by_id.get(source_id)

            if old:
                entry = dict(old)
                entry.update(base)
                entry["ok"] = False
                entry["stale"] = True
                entry["error"] = clean(errors.get(source_id)) or "API lỗi"
                sources["sources"].append(entry)
                continue

            current = []

        leagues = build_source_leagues(current)

        sources["sources"].append({
            **base,
            "ok": source_id not in errors,
            "stale": False,
            "error": "",
            "match_count": sum(len(g["matches"]) for g in leagues),
            "stream_count": sum(
                m["stream_count"]
                for g in leagues for m in g["matches"]
            ),
            "leagues": leagues
        })

    return sources


# ============================================================
# MAIN UPDATE
# ============================================================

def main():
    apis = [
        api for api in CONFIG.get("apis", [])
        if api.get("enabled", True)
    ]

    if not apis:
        log.error("Không có API nào được bật trong config.json")
        return 1

    previous_sources = load_previous_sources()

    api_results = {}
    all_matches = []

    success_count = 0
    failed_count = 0
    api_errors = {}

    for api in apis:
        name = api.get("name", api.get("id", "API"))
        source_id = clean(api.get("id"))

        log.info("Đang cập nhật: %s", name)

        try:
            raw_items = fetch_api(api)

            matches = parse_api_items(
                raw_items,
                api
            )

            for match in matches:
                match["_source"] = {
                    "id": source_id,
                    "name": clean(
                        api.get("display_name") or name
                    ),
                    "logo": clean(
                        api.get("logo")
                        or (
                            "{}/{}.png".format(
                                CONFIG["source_logo_base"].rstrip("/"),
                                source_id
                            )
                            if CONFIG.get("source_logo_base")
                            else ""
                        )
                    )
                }

            api_results[source_id] = matches
            all_matches.extend(matches)

            success_count += 1

            log.info(
                "%s: nhận %s bản ghi, %s trận có luồng",
                name,
                len(raw_items),
                len([
                    m for m in matches
                    if collect_playlist_entries([m])
                ])
            )

        except Exception as exc:
            failed_count += 1
            api_results[source_id] = None
            api_errors[source_id] = str(exc)[:300]

            log.error(
                "%s: %s",
                name,
                exc
            )

    all_entries = collect_playlist_entries(all_matches)

    # Avoid overwriting valid outputs when every API fails.
    if success_count == 0:
        log.error(
            "Tất cả API đều lỗi. Giữ nguyên dữ liệu cũ."
        )
        return 1

    if not all_entries:
        log.error(
            "Không tìm thấy luồng hợp lệ. "
            "Giữ nguyên playlist hiện tại."
        )

        # Still update sources status for diagnosis.
        source_data = build_sources(
            api_results,
            previous_sources,
            api_errors
        )

        atomic_write_json(
            SOURCES_FILE,
            source_data
        )

        return 1

    # Global playlist + EPG (API lỗi thì giữ dữ liệu cũ của riêng nguồn đó).
    global_m3u, global_epg, fresh_count, stale_count = (
        build_global_outputs(apis, api_results)
    )

    atomic_write(OUTPUT_FILE, global_m3u)

    log.info(
        "Đã tạo %s (%s luồng mới, %s luồng giữ từ lần trước)",
        OUTPUT_FILE.name,
        fresh_count,
        stale_count
    )

    # Per-source playlists.
    for api in apis:
        source_id = clean(api.get("id"))
        matches = api_results.get(source_id)

        if matches is None:
            log.warning(
                "%s: lỗi API, không ghi đè playlist riêng",
                source_id
            )
            continue

        entries = collect_playlist_entries(matches)

        if not entries:
            log.info(
                "%s: không có luồng, bỏ qua playlist riêng",
                source_id
            )
            continue

        playlist_name = api.get(
            "playlist_file",
            f"thethao-{source_id}.m3u"
        )

        playlist_path = ROOT / playlist_name

        atomic_write(
            playlist_path,
            generate_m3u(
                entries,
                api.get("logo", "")
            )
        )

        log.info(
            "Đã tạo %s (%s luồng)",
            playlist_name,
            len(entries)
        )

    # XMLTV EPG.
    atomic_write(EPG_FILE, global_epg)

    log.info("Đã tạo %s", EPG_FILE.name)

    # sources.json.
    source_data = build_sources(
        api_results,
        previous_sources,
        api_errors
    )

    atomic_write_json(
        SOURCES_FILE,
        source_data
    )

    log.info("Đã cập nhật %s", SOURCES_FILE.name)

    if failed_count:
        log.warning(
            "Hoàn tất một phần: %s API thành công, %s API lỗi",
            success_count,
            failed_count
        )
    else:
        log.info("Tất cả API đã cập nhật thành công.")

    save_referer_cache()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except KeyboardInterrupt:
        log.warning("Đã dừng theo yêu cầu.")
        raise SystemExit(130)

    except Exception:
        log.exception("Lỗi không xác định")
        raise SystemExit(1)
