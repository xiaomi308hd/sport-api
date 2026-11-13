
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MHD SPORTS - API UPDATE

Nguồn:
- ChuoiChienTV
- GaVangTV
- GioVangTV
- PhaLangTV / PLAPI Graph
- ColaTV

Chức năng:
- Đọc cấu hình động từ config.json
- GET / POST
- Params variants
- PLAPI Graph pagination + detail + live
- Giờ Vàng detail resolver
- CoLa TV HLS / FLV / servers
- Referer và headers theo host
- Playlist tổng và playlist riêng
- EPG XML và sources.json
- Giữ playlist cũ khi API lỗi
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import datetime, timezone, timedelta


# ============================================================
# 1. CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"

VN_TZ = timezone(timedelta(hours=7))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s"
)
log = logging.getLogger("MHD-Sports")


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

OUTPUT_FILE = ROOT / CONFIG.get("output_file", "thethao.m3u")
EPG_FILE = ROOT / CONFIG.get("epg_file", "epg.xml")
SOURCES_FILE = ROOT / CONFIG.get("sources_file", "sources.json")


# ============================================================
# 2. GENERAL HELPERS
# ============================================================

def clean(value):
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return ""

    return str(value).strip()


def first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def safe_name(value):
    value = clean(value).lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    return value.strip("-") or "source"


def write_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_suffix(path.suffix + ".tmp")

    with open(temp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    os.replace(temp, path)


def write_json(path, data):
    write_file(
        path,
        json.dumps(data, ensure_ascii=False, indent=2)
    )


def parse_datetime(value):
    if not value:
        return None

    try:
        if isinstance(value, (int, float)):
            if value > 9999999999:
                value = value / 1000

            return datetime.fromtimestamp(
                value,
                tz=timezone.utc
            )

        value = clean(value)

        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M"
    ):
        try:
            return datetime.strptime(
                clean(value), fmt
            ).replace(tzinfo=timezone.utc)

        except Exception:
            continue

    return None


def source_id(api):
    return clean(api.get("id")).lower()


def source_name(api):
    return clean(
        api.get("display_name")
        or api.get("name")
        or api.get("id")
        or "API"
    )


def get_parser(api):
    parser = clean(api.get("parser")).lower()

    if parser:
        return parser

    api_type = clean(api.get("api_type")).lower()

    if api_type:
        return api_type

    return source_id(api)


def merge_dicts(base, extra):
    result = dict(base or {})

    if not isinstance(extra, dict):
        return result

    for key, value in extra.items():
        if value is None or value == "":
            continue

        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        elif value != []:
            result[key] = value

    return result


# ============================================================
# 3. HTTP REQUEST
# ============================================================

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/157.0.0.0 Safari/537.36"
)


def request_json(
    url,
    method="GET",
    headers=None,
    params=None,
    body=None
):
    method = clean(method).upper() or "GET"
    headers = dict(headers or {})

    headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    headers.setdefault("Accept", "*/*")

    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url += ("&" if "?" in url else "?") + query

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
                timeout=TIMEOUT
            ) as response:

                raw = response.read().decode(
                    "utf-8",
                    errors="replace"
                )

                if not raw.strip():
                    return None

                return json.loads(raw)

        except urllib.error.HTTPError as exc:

            error_body = ""

            try:
                error_body = exc.read().decode(
                    "utf-8",
                    errors="replace"
                )[:500]
            except Exception:
                pass

            if exc.code in (
                408, 425, 429,
                500, 502, 503, 504
            ):
                if attempt < RETRIES:
                    time.sleep(min(2 ** attempt, 10))
                    continue

            raise RuntimeError(
                f"HTTP {exc.code}: {exc.reason} {error_body}"
            )

        except Exception as exc:

            if attempt < RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue

            raise RuntimeError(str(exc))

    raise RuntimeError("Không thể kết nối API")


def request_api(api, url=None, method=None, params=None, body=None):
    return request_json(
        url or api["url"],
        method=method or api.get("method", "GET"),
        headers=api.get("headers", {}),
        params=params,
        body=body
    )


# ============================================================
# 4. EXTRACT API DATA
# ============================================================

LIST_KEYS = (
    "matches",
    "items",
    "results",
    "fixtures",
    "data",
    "records",
    "list"
)


def unwrap_data(payload):
    """
    Hỗ trợ:
    - Root array
    - data array
    - data object
    - matches/items/results
    - GraphQL data
    """

    if isinstance(payload, list):
        return [
            item for item in payload
            if isinstance(item, dict)
        ]

    if not isinstance(payload, dict):
        return []

    for key in LIST_KEYS:
        value = payload.get(key)

        if isinstance(value, list):
            return [
                item for item in value
                if isinstance(item, dict)
            ]

    # GraphQL hoặc API lồng data.
    data = payload.get("data")

    if isinstance(data, dict):

        for key in LIST_KEYS:
            value = data.get(key)

            if isinstance(value, list):
                return [
                    item for item in value
                    if isinstance(item, dict)
                ]

        # Một số API trả object theo ID.
        values = [
            value for value in data.values()
            if isinstance(value, dict)
        ]

        if values:
            return values

    # Tìm danh sách trận trong các object con.
    for value in payload.values():

        if isinstance(value, dict):
            result = unwrap_data(value)

            if result:
                return result

    return []


def recursive_find_values(obj, keys):
    """
    Tìm các trường trong JSON lồng nhau.
    Trả về danh sách (key, value).
    """

    result = []

    if isinstance(obj, dict):

        for key, value in obj.items():

            if key in keys:
                result.append((key, value))

            if isinstance(value, (dict, list)):
                result.extend(
                    recursive_find_values(value, keys)
                )

    elif isinstance(obj, list):

        for value in obj:
            result.extend(
                recursive_find_values(value, keys)
            )

    return result


def recursive_find_first(obj, keys):
    values = recursive_find_values(obj, keys)

    for key, value in values:
        if value is not None and value != "":
            return value

    return None


# ============================================================
# 5. STREAM NORMALIZATION
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
            stream.get("playUrl"),
            stream.get("stream_url"),
            stream.get("streamUrl"),
            stream.get("playStreamAddress2"),
            stream.get("playStreamAddress")
        )

        label = first_value(
            stream.get("label"),
            stream.get("name"),
            stream.get("quality"),
            stream.get("title"),
            "Live"
        )

        headers = dict(stream.get("headers") or {})

    else:
        return None

    url = clean(url)

    if not url.startswith(("http://", "https://")):
        return None

    if default_referer:
        has_referer = any(
            key.lower() == "referer"
            for key in headers
        )

        if not has_referer:
            headers["Referer"] = default_referer

    return {
        "url": url,
        "label": clean(label),
        "headers": headers
    }


def unique_streams(streams):
    result = []
    seen = set()

    for stream in streams:

        if not stream:
            continue

        url = stream["url"]

        if url in seen:
            continue

        seen.add(url)
        result.append(stream)

    return result


def extract_streams_from_object(obj, api, default_label="Live"):
    """
    Đọc stream theo stream_priority trong config.
    Có hỗ trợ URL nằm trong object hoặc array.
    """

    priority = api.get("stream_priority") or [
        "url", "link", "source_live",
        "source", "streams", "sources"
    ]

    referer = api.get("default_stream_referer", "")
    streams = []

    def add_value(value, label):

        if isinstance(value, list):
            for index, child in enumerate(value, start=1):
                add_value(child, f"{label} {index}")

        elif isinstance(value, dict):

            stream = normalize_stream(
                value,
                referer
            )

            if stream:
                if stream["label"] == "Live":
                    stream["label"] = label

                streams.append(stream)

            else:
                for key, child in value.items():
                    if isinstance(child, (str, dict, list)):
                        add_value(child, clean(key) or label)

        elif isinstance(value, str):

            stream = normalize_stream(
                {
                    "url": value,
                    "label": label or default_label
                },
                referer
            )

            if stream:
                streams.append(stream)

    for key in priority:

        if not isinstance(obj, dict):
            break

        if key in obj:
            add_value(obj[key], key)

    return unique_streams(streams)


# ============================================================
# 6. GENERIC MATCH PARSER
# ============================================================

def parse_generic_match(item, api):
    if not isinstance(item, dict):
        return None

    home = first_value(
        item.get("homeTeamName"),
        item.get("home_team"),
        item.get("home"),
        item.get("team_1"),
        item.get("team1"),
        "Đội nhà"
    )

    away = first_value(
        item.get("awayTeamName"),
        item.get("away_team"),
        item.get("away"),
        item.get("team_2"),
        item.get("team2"),
        "Đội khách"
    )

    league = first_value(
        item.get("competitionName"),
        item.get("league_name"),
        item.get("league"),
        item.get("competition"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = first_value(
            league.get("name"),
            league.get("title"),
            "Thể thao"
        )

    streams = extract_streams_from_object(item, api)

    return {
        "externalId": clean(first_value(
            item.get("id"),
            item.get("match_id"),
            item.get("matchId"),
            item.get("fixture_id")
        )),

        "matchTime": clean(first_value(
            item.get("matchTime"),
            item.get("match_time"),
            item.get("startTime"),
            item.get("start_date"),
            item.get("date"),
            item.get("timestamp")
        )),

        "status": clean(first_value(
            item.get("status"),
            item.get("matchStatus"),
            item.get("match_status")
        )),

        "sport": clean(first_value(
            item.get("sport"),
            item.get("sportName"),
            item.get("sport_name")
        )),

        "score": clean(first_value(
            item.get("score"),
            item.get("result")
        )),

        "teams": {
            "home": {
                "name": clean(home),
                "logo": clean(first_value(
                    item.get("homeTeamLogo"),
                    item.get("home_logo"),
                    item.get("team_1_logo")
                ))
            },

            "away": {
                "name": clean(away),
                "logo": clean(first_value(
                    item.get("awayTeamLogo"),
                    item.get("away_logo"),
                    item.get("team_2_logo")
                ))
            }
        },

        "league": {
            "name": clean(league),
            "logo": clean(first_value(
                item.get("competitionLogo"),
                item.get("league_logo")
            ))
        },

        "blvs": [{
            "name": source_name(api),
            "streams": streams
        }] if streams else []
    }


# ============================================================
# 7. GA VANG TV PARSER
# ============================================================

def parse_gavang_match(item, api):
    if not isinstance(item, dict):
        return None

    referer = api.get("default_stream_referer", "")
    streams = []

    appointments = item.get(
        "anchorAppointmentVoList",
        []
    )

    if isinstance(appointments, list):

        for appointment in appointments:

            if not isinstance(appointment, dict):
                continue

            values = appointment.get("streamUrls", [])

            if isinstance(values, list):

                for value in values:
                    stream = normalize_stream(value, referer)

                    if stream:
                        streams.append(stream)

    if not streams:
        generic = parse_generic_match(item, api)

        if generic:
            return generic

    league = first_value(
        item.get("competition"),
        item.get("league"),
        item.get("competitionName"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = league.get("name", "Thể thao")

    return {
        "externalId": clean(first_value(
            item.get("id"),
            item.get("matchId")
        )),

        "matchTime": clean(first_value(
            item.get("startTime"),
            item.get("matchTime"),
            item.get("date")
        )),

        "status": clean(first_value(
            item.get("status"),
            item.get("matchStatus")
        )),

        "teams": {
            "home": {
                "name": clean(first_value(
                    item.get("homeTeamName"),
                    item.get("homeTeam"),
                    item.get("home_team"),
                    "Đội nhà"
                )),
                "logo": clean(item.get("homeTeamLogo"))
            },

            "away": {
                "name": clean(first_value(
                    item.get("awayTeamName"),
                    item.get("awayTeam"),
                    item.get("away_team"),
                    "Đội khách"
                )),
                "logo": clean(item.get("awayTeamLogo"))
            }
        },

        "league": {
            "name": clean(league),
            "logo": clean(item.get("competitionLogo"))
        },

        "blvs": [{
            "name": source_name(api),
            "streams": unique_streams(streams)
        }] if streams else []
    }


# ============================================================
# 8. CHUOI CHIEN TV PARSER
# ============================================================

def parse_chuoichien_match(item, api):
    if not isinstance(item, dict):
        return None

    referer = api.get("default_stream_referer", "")

    home = first_value(
        item.get("homeTeamName"),
        item.get("home_team"),
        item.get("home"),
        item.get("team_1"),
        "Đội nhà"
    )

    away = first_value(
        item.get("awayTeamName"),
        item.get("away_team"),
        item.get("away"),
        item.get("team_2"),
        "Đội khách"
    )

    league = first_value(
        item.get("league_name"),
        item.get("league"),
        item.get("competition"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = league.get("name", "Thể thao")

    blvs = []

    group_keys = api.get(
        "include_stream_groups",
        CONFIG.get(
            "include_stream_groups",
            ["blvs", "blvs_bonglau", "blvs_nguoitho"]
        )
    )

    for key in group_keys:

        group = item.get(key)

        if not isinstance(group, list):
            continue

        for blv in group:

            if not isinstance(blv, dict):
                continue

            nickname = clean(first_value(
                blv.get("name"),
                blv.get("blv_name"),
                blv.get("nickname"),
                blv.get("title"),
                "BLV"
            ))

            streams = []

            for field in (
                "streams",
                "streamUrls",
                "sources",
                "links"
            ):

                values = blv.get(field)

                if isinstance(values, list):

                    for value in values:
                        stream = normalize_stream(value, referer)

                        if stream:
                            streams.append(stream)

            for field in (
                "hd1", "hd2", "hd_1", "hd_2",
                "fhd", "sd", "flv", "hls",
                "url", "link", "source"
            ):

                value = blv.get(field)

                if isinstance(value, dict):
                    value = first_value(
                        value.get("url"),
                        value.get("link"),
                        value.get("source")
                    )

                if isinstance(value, str):

                    stream = normalize_stream(
                        {
                            "url": value,
                            "label": field.upper()
                        },
                        referer
                    )

                    if stream:
                        streams.append(stream)

            streams = unique_streams(streams)

            if streams:
                blvs.append({
                    "name": nickname,
                    "streams": streams
                })

    if not blvs:
        generic = parse_generic_match(item, api)

        if generic:
            blvs = generic.get("blvs", [])

    return {
        "externalId": clean(first_value(
            item.get("id"),
            item.get("match_id"),
            item.get("matchId")
        )),

        "matchTime": clean(first_value(
            item.get("matchTime"),
            item.get("match_time"),
            item.get("startTime"),
            item.get("start_date"),
            item.get("date")
        )),

        "status": clean(item.get("status")),

        "sport": clean(item.get("sport")),

        "teams": {
            "home": {
                "name": clean(home),
                "logo": clean(first_value(
                    item.get("home_logo"),
                    item.get("homeTeamLogo")
                ))
            },

            "away": {
                "name": clean(away),
                "logo": clean(first_value(
                    item.get("away_logo"),
                    item.get("awayTeamLogo")
                ))
            }
        },

        "league": {
            "name": clean(league),
            "logo": clean(item.get("league_logo"))
        },

        "blvs": blvs
    }


# ============================================================
# 9. COLA TV PARSER
# ============================================================

def get_colatv_server_url(server):
    if isinstance(server, str):
        return server

    if isinstance(server, dict):
        return first_value(
            server.get("url"),
            server.get("link"),
            server.get("src"),
            server.get("source"),
            server.get("playStreamAddress2"),
            server.get("playStreamAddress"),
            server.get("playUrl")
        )

    return ""


def parse_colatv_match(item, api):
    if not isinstance(item, dict):
        return None

    referer = api.get(
        "default_stream_referer",
        "https://colatv98.live/"
    )

    blvs = []
    seen_urls = set()

    def add_stream(target, url, label):

        url = clean(url)

        if not url.startswith(("http://", "https://")):
            return

        if url in seen_urls:
            return

        stream = normalize_stream(
            {
                "url": url,
                "label": label
            },
            referer
        )

        if stream:
            target.append(stream)
            seen_urls.add(url)

    main_streams = []

    add_stream(
        main_streams,
        item.get("videoUrl"),
        "Luồng chính HLS"
    )

    if main_streams:
        blvs.append({
            "name": "CoLa TV",
            "streams": main_streams
        })

    appointments = item.get(
        "anchorAppointmentVoList",
        []
    )

    if isinstance(appointments, list):

        for index, appointment in enumerate(
            appointments,
            start=1
        ):

            if not isinstance(appointment, dict):
                continue

            nickname = clean(first_value(
                appointment.get("nickName"),
                appointment.get("houseName"),
                appointment.get("name"),
                f"BLV {index}"
            ))

            streams = []

            add_stream(
                streams,
                appointment.get("playStreamAddress2"),
                "HLS"
            )

            servers = appointment.get("servers", [])

            if isinstance(servers, list):

                for server_index, server in enumerate(
                    servers,
                    start=1
                ):

                    add_stream(
                        streams,
                        get_colatv_server_url(server),
                        f"Server {server_index}"
                    )

            elif isinstance(servers, dict):

                for server_name, server in servers.items():

                    add_stream(
                        streams,
                        get_colatv_server_url(server),
                        clean(server_name) or "Server"
                    )

            add_stream(
                streams,
                appointment.get("playStreamAddress"),
                "FLV"
            )

            if streams:
                blvs.append({
                    "name": nickname,
                    "streams": streams
                })

    def score_text(value):
        if isinstance(value, list):
            return " - ".join(
                clean(v) for v in value if clean(v)
            )

        return clean(value)

    home_score = score_text(item.get("homeScore"))
    away_score = score_text(item.get("awayScore"))

    score = ""

    if home_score or away_score:
        score = f"{home_score} - {away_score}"

    return {
        "externalId": clean(item.get("matchId")),

        "matchTime": clean(item.get("matchTime")),

        "status": clean(item.get("matchStatus")),

        "sport": clean(first_value(
            item.get("sportName"),
            item.get("sport"),
            item.get("sportId")
        )),

        "score": score,

        "teams": {
            "home": {
                "name": clean(first_value(
                    item.get("homeTeamName"),
                    "Đội nhà"
                )),
                "logo": clean(item.get("homeTeamLogo"))
            },

            "away": {
                "name": clean(first_value(
                    item.get("awayTeamName"),
                    "Đội khách"
                )),
                "logo": clean(item.get("awayTeamLogo"))
            }
        },

        "league": {
            "name": clean(first_value(
                item.get("competitionName"),
                "Thể thao"
            )),
            "logo": clean(item.get("competitionLogo"))
        },

        "blvs": blvs
    }


# ============================================================
# 10. GIO VANG TV - DETAIL RESOLVER
# ============================================================

GIOVANG_STREAM_FIELDS = (
    "pc_stream_url",
    "mobile_stream_url",
    "link_stream_hd",
    "link_stream_sd"
)


def get_item_id(item):
    return clean(first_value(
        item.get("id"),
        item.get("match_id"),
        item.get("matchId"),
        item.get("fixture_id"),
        item.get("fixtureId")
    ))


def format_endpoint(template, item):
    match_id = get_item_id(item)

    if not template or not match_id:
        return ""

    try:
        return template.format(
            id=urllib.parse.quote(match_id, safe="")
        )
    except Exception:
        return ""


def fetch_giovang(api):
    """
    Giờ Vàng:
    1. Gọi live.json.
    2. Tìm ID trận.
    3. Gọi match_detail_url.
    4. Đọc các trường stream_priority.
    """

    payload = request_api(api)
    items = unwrap_data(payload)

    if not items and isinstance(payload, dict):
        # Một số phiên bản trả trực tiếp một object trận.
        if get_item_id(payload):
            items = [payload]

    detail_url = api.get("match_detail_url", "")
    max_details = int(api.get("max_detail_requests", 100))

    detail_count = 0
    result = []

    for item in items:

        if not isinstance(item, dict):
            continue

        merged = dict(item)

        streams = extract_streams_from_object(
            merged,
            api
        )

        match_id = get_item_id(item)

        if not streams and detail_url and match_id:
            if detail_count < max_details:

                url = format_endpoint(detail_url, item)

                if url:
                    try:
                        detail_payload = request_api(
                            api,
                            url=url,
                            method="GET"
                        )

                        detail_items = unwrap_data(detail_payload)

                        detail = (
                            detail_items[0]
                            if detail_items
                            else detail_payload
                        )

                        if isinstance(detail, dict):
                            merged = merge_dicts(merged, detail)

                        detail_count += 1

                        streams = extract_streams_from_object(
                            merged,
                            api
                        )

                    except Exception as exc:
                        log.warning(
                            "Giờ Vàng: lỗi lấy chi tiết trận %s: %s",
                            match_id,
                            exc
                        )

            else:
                log.warning(
                    "Giờ Vàng: đã đạt giới hạn %s lượt detail",
                    max_details
                )

        match = parse_generic_match(merged, api)

        if match:
            if streams:
                match["blvs"] = [{
                    "name": source_name(api),
                    "streams": streams
                }]

            result.append(match)

    log.info(
        "Giờ Vàng: đã gọi %s API chi tiết",
        detail_count
    )

    return result


# ============================================================
# 11. PLAPI GRAPH - PAGINATION
# ============================================================

def replace_body_pagination(body, page, limit):
    result = dict(body or {})
    result["page"] = page
    result["limit"] = limit
    return result


def fetch_plapi_pages(api):
    """
    PLAPI:
    - POST /matches/graph
    - Phân trang theo config.pagination
    """

    pagination = api.get("pagination", {})

    enabled = pagination.get("enabled", False)

    if not enabled:
        payload = request_api(api)
        return unwrap_data(payload)

    start_page = int(pagination.get("start_page", 1))
    limit = int(pagination.get("limit", 50))
    max_pages = int(pagination.get("max_pages", 100))

    page_param = pagination.get("page_param", "page")
    limit_param = pagination.get("limit_param", "limit")

    base_body = dict(api.get("body") or {})

    all_items = []
    seen_ids = set()

    for page_index in range(max_pages):

        page = start_page + page_index

        body = dict(base_body)
        body[page_param] = page
        body[limit_param] = limit

        payload = request_api(
            api,
            method=api.get("method", "POST"),
            body=body
        )

        items = unwrap_data(payload)

        if not items:
            if page_index == 0:
                log.warning(
                    "PLAPI: trang đầu không có dữ liệu."
                )
            break

        new_count = 0

        for item in items:

            if not isinstance(item, dict):
                continue

            match_id = get_item_id(item)

            if match_id:
                if match_id in seen_ids:
                    continue

                seen_ids.add(match_id)

            all_items.append(item)
            new_count += 1

        log.info(
            "PLAPI: trang %s, nhận %s trận mới",
            page,
            new_count
        )

        if len(items) < limit:
            break

        if new_count == 0:
            break

    log.info(
        "PLAPI: tổng số trận qua phân trang: %s",
        len(all_items)
    )

    return all_items


def fetch_plapi_detail(api, item, endpoint_key, method_key):
    template = api.get(endpoint_key, "")

    if not template:
        return None

    url = format_endpoint(template, item)

    if not url:
        return None

    method = api.get(method_key, "GET")

    return request_api(
        api,
        url=url,
        method=method
    )


def merge_api_payload(item, payload):
    if not isinstance(payload, dict):
        return item

    values = unwrap_data(payload)

    if values:
        detail = values[0]
    else:
        detail = payload

    if isinstance(detail, dict):
        return merge_dicts(item, detail)

    return item


def fetch_plapi(api):
    """
    PLAPI:
    1. Lấy danh sách bằng Graph API.
    2. Đọc stream trực tiếp nếu có.
    3. Nếu chưa có stream, gọi detail và live.
    """

    items = fetch_plapi_pages(api)

    result = []

    detail_count = 0

    max_details = int(
        api.get("max_detail_requests", 500)
    )

    for item in items:

        if not isinstance(item, dict):
            continue

        merged = dict(item)

        streams = extract_streams_from_object(
            merged,
            api
        )

        match_id = get_item_id(item)

        if not streams and match_id and detail_count < max_details:

            # API chi tiết trận.
            if api.get("match_detail_url"):

                try:
                    detail_payload = fetch_plapi_detail(
                        api,
                        item,
                        "match_detail_url",
                        "detail_method"
                    )

                    merged = merge_api_payload(
                        merged,
                        detail_payload
                    )

                    streams = extract_streams_from_object(
                        merged,
                        api
                    )

                except Exception as exc:
                    log.warning(
                        "PLAPI detail %s lỗi: %s",
                        match_id,
                        exc
                    )

            # API live.
            if not streams and api.get("match_live_url"):

                try:
                    live_payload = fetch_plapi_detail(
                        api,
                        item,
                        "match_live_url",
                        "live_method"
                    )

                    merged = merge_api_payload(
                        merged,
                        live_payload
                    )

                    streams = extract_streams_from_object(
                        merged,
                        api
                    )

                except Exception as exc:
                    log.warning(
                        "PLAPI live %s lỗi: %s",
                        match_id,
                        exc
                    )

            detail_count += 1

        match = parse_plapi_match(merged, api)

        if match:
            if streams:
                match["blvs"] = [{
                    "name": source_name(api),
                    "streams": streams
                }]

            result.append(match)

    log.info(
        "PLAPI: đã xử lý %s trận, gọi detail/live cho %s trận",
        len(result),
        detail_count
    )

    return result


# ============================================================
# 12. PLAPI MATCH PARSER
# ============================================================

def parse_plapi_match(item, api):
    if not isinstance(item, dict):
        return None

    status = clean(first_value(
        item.get("status"),
        item.get("match_status")
    )).lower()

    if status in (
        "finished",
        "ended",
        "completed",
        "ft"
    ):
        return None

    home = first_value(
        item.get("team_1"),
        item.get("home"),
        item.get("homeTeamName"),
        "Đội nhà"
    )

    away = first_value(
        item.get("team_2"),
        item.get("away"),
        item.get("awayTeamName"),
        "Đội khách"
    )

    league = first_value(
        item.get("league"),
        item.get("competitionName"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = league.get("name", "Thể thao")

    streams = extract_streams_from_object(item, api)

    return {
        "externalId": get_item_id(item),

        "matchTime": clean(first_value(
            item.get("start_date"),
            item.get("matchTime"),
            item.get("date"),
            item.get("start_time")
        )),

        "status": status,

        "sport": clean(first_value(
            item.get("sport"),
            item.get("sportName")
        )),

        "teams": {
            "home": {
                "name": clean(home),
                "logo": clean(first_value(
                    item.get("team_1_logo"),
                    item.get("homeTeamLogo")
                ))
            },

            "away": {
                "name": clean(away),
                "logo": clean(first_value(
                    item.get("team_2_logo"),
                    item.get("awayTeamLogo")
                ))
            }
        },

        "league": {
            "name": clean(league),
            "logo": clean(first_value(
                item.get("league_logo"),
                item.get("competitionLogo")
            ))
        },

        "blvs": [{
            "name": source_name(api),
            "streams": streams
        }] if streams else []
    }


# ============================================================
# 13. FETCH API DISPATCH
# ============================================================

def fetch_api(api):
    parser = get_parser(api)

    if parser in ("giovangtv", "giovang"):
        return fetch_giovang(api)

    if parser in ("plapi_graph", "plapi", "phalangtv"):
        return fetch_plapi(api)

    if parser in ("chuoichientv", "chuoichien"):
        variants = api.get("params_variants", [])

        if not variants:
            variants = [api.get("params", {})]

        all_items = []
        seen = set()

        for index, params in enumerate(variants, start=1):

            try:
                payload = request_api(
                    api,
                    params=params
                )

                items = unwrap_data(payload)

                log.info(
                    "%s: params variant %s nhận %s bản ghi",
                    source_name(api),
                    index,
                    len(items)
                )

                for item in items:
                    match_id = get_item_id(item)

                    key = match_id or json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True
                    )

                    if key in seen:
                        continue

                    seen.add(key)
                    all_items.append(item)

            except Exception as exc:
                log.warning(
                    "%s: params variant %s lỗi: %s",
                    source_name(api),
                    index,
                    exc
                )

        if all_items:
            return all_items

        raise RuntimeError(
            "Không lấy được dữ liệu từ bất kỳ params variant nào"
        )

    # Các nguồn còn lại.
    payload = request_api(api)
    return unwrap_data(payload)


# ============================================================
# 14. PARSER DISPATCH
# ============================================================

def parse_api_items(items, api):
    parser = get_parser(api)

    result = []

    for item in items:

        try:

            if parser in ("colatv",):
                match = parse_colatv_match(item, api)

            elif parser in ("gavangtv", "gavang"):
                match = parse_gavang_match(item, api)

            elif parser in ("chuoichientv", "chuoichien"):
                match = parse_chuoichien_match(item, api)

            elif parser in ("plapi_graph", "plapi", "phalangtv"):
                match = parse_plapi_match(item, api)

            elif parser in ("giovangtv", "giovang"):
                match = parse_generic_match(item, api)

            else:
                match = parse_generic_match(item, api)

            if match:
                result.append(match)

        except Exception as exc:
            log.warning(
                "%s: bỏ qua trận lỗi: %s",
                source_name(api),
                exc
            )

    return result


# ============================================================
# 15. GLOBAL STREAM HEADER RULES
# ============================================================

def get_host(url):
    try:
        return (
            urllib.parse.urlparse(url).hostname or ""
        ).lower()
    except Exception:
        return ""


def apply_stream_header_rules(matches, api):
    """
    Áp dụng:
    - headers trong stream
    - referer_by_host của API
    - stream_header_rules toàn cục
    - stream_referer của API
    - default_stream_referer
    """

    common_referer = clean(
        api.get("stream_referer")
        or api.get("default_stream_referer")
    )

    host_rules = api.get("referer_by_host", {})

    global_rules = CONFIG.get(
        "stream_header_rules",
        []
    )

    for match in matches:

        for blv in match.get("blvs", []):

            for stream in blv.get("streams", []):

                headers = stream.setdefault("headers", {})
                host = get_host(stream["url"])

                # Header theo host riêng của API.
                api_headers = host_rules.get(host, {})

                if isinstance(api_headers, dict):
                    headers.update(api_headers)

                # Header theo host toàn cục.
                for rule in global_rules:

                    if clean(rule.get("host")).lower() == host:

                        rule_headers = rule.get("headers", {})

                        if isinstance(rule_headers, dict):
                            headers.update(rule_headers)

                # Referer chung nếu chưa có.
                has_referer = any(
                    key.lower() == "referer"
                    for key in headers
                )

                if not has_referer and common_referer:
                    headers["Referer"] = common_referer

                # Tự động dùng Referer API nếu bật.
                if (
                    not has_referer
                    and CONFIG.get("auto_detect_referers", False)
                ):

                    api_referer = (
                        api.get("headers", {}).get("Referer")
                        or api.get("headers", {}).get("referer")
                    )

                    if api_referer:
                        headers["Referer"] = api_referer


def attach_api_referers(matches, api):
    apply_stream_header_rules(matches, api)


# ============================================================
# 16. SPORT DETECTION
# ============================================================

SPORT_PATTERNS = {
    "BASKETBALL": (
        "nba", "wnba", "basketball", "bóng rổ",
        "bong ro", "euroleague", "fiba", "vba"
    ),

    "VOLLEYBALL": (
        "volleyball", "bóng chuyền",
        "bong chuyen", "vnl", "fivb"
    ),

    "TENNIS": (
        "tennis", "atp", "wta", "wimbledon",
        "roland garros", "us open"
    ),

    "BADMINTON": (
        "badminton", "cầu lông",
        "cau long", "bwf"
    ),

    "BILLIARDS": (
        "billiard", "billiards", "snooker",
        "bida", "bi-a", "pool"
    ),

    "ESPORTS": (
        "esports", "e-sports", "esport",
        "league of legends", "lck", "lec",
        "dota 2", "valorant", "vct",
        "pubg", "mobile legends", "mlbb",
        "liên quân", "lien quan", "cs2"
    ),

    "FOOTBALL": (
        "football", "soccer", "bóng đá",
        "bong da", "premier league", "la liga",
        "serie a", "bundesliga", "ligue 1",
        "champions league", "uefa", "fifa",
        "world cup", "afc", "v-league", "mls"
    )
}


def match_sport(match):
    text = " ".join([
        clean(match.get("sport")),
        clean(match.get("league", {}).get("name")),
        clean(match.get("teams", {}).get("home", {}).get("name")),
        clean(match.get("teams", {}).get("away", {}).get("name"))
    ]).lower()

    for sport, keywords in SPORT_PATTERNS.items():

        if any(keyword in text for keyword in keywords):
            return sport

    return "FOOTBALL"


# ============================================================
# 17. M3U GENERATOR
# ============================================================

def match_title(match):
    teams = match["teams"]

    return "{} vs {}".format(
        teams["home"]["name"],
        teams["away"]["name"]
    )


def match_tvg_id(match):
    external_id = clean(match.get("externalId"))

    if external_id:
        return "mhd-match-" + external_id

    return "mhd-match-" + safe_name(match_title(match))


def format_match_time(value):
    dt = parse_datetime(value)

    if not dt:
        return ""

    return dt.astimezone(VN_TZ).strftime("%d/%m %H:%M")


def m3u_escape(value):
    return clean(value).replace('"', "'").replace("\n", " ")


def make_extinf(match, blv_name, stream, api):
    teams = match["teams"]
    league = match["league"]

    title = match_title(match)

    time_label = format_match_time(
        match.get("matchTime")
    )

    parts = [
        title,
        time_label,
        clean(blv_name),
        clean(stream.get("label"))
    ]

    display_title = " | ".join(
        part for part in parts if part
    )

    logo = first_value(
        league.get("logo"),
        teams["home"].get("logo"),
        teams["away"].get("logo"),
        api.get("logo")
    )

    attrs = [
        f'tvg-id="{m3u_escape(match_tvg_id(match))}"',
        f'tvg-name="{m3u_escape(title)}"',
        f'tvg-logo="{m3u_escape(logo)}"',
        f'group-title="{m3u_escape(league.get("name"))}"',
        f'x-source="{m3u_escape(api.get("id"))}"',
        f'x-sport="{match_sport(match)}"'
    ]

    return (
        "#EXTINF:-1 "
        + " ".join(attrs)
        + ","
        + m3u_escape(display_title)
    )


def make_header_lines(stream):
    lines = []

    headers = stream.get("headers", {})

    for key, value in headers.items():

        original_key = clean(key)
        lower_key = original_key.lower()
        value = clean(value)

        if not value:
            continue

        if lower_key == "referer":
            lines.append(
                "#EXTVLCOPT:http-referrer=" + value
            )

        elif lower_key == "user-agent":
            lines.append(
                "#EXTVLCOPT:http-user-agent=" + value
            )

        else:
            lines.append(
                "#EXTHTTP:"
                + json.dumps(
                    {original_key: value},
                    ensure_ascii=False
                )
            )

    return lines


def collect_entries(matches, api):
    entries = []

    for match in matches:

        for blv in match.get("blvs", []):

            for stream in blv.get("streams", []):

                entries.append({
                    "match": match,
                    "blv": blv.get("name", "Live"),
                    "stream": stream,
                    "api": api
                })

    return entries


def generate_m3u(entries):
    lines = ["#EXTM3U"]
    seen = set()

    for entry in entries:

        stream = entry["stream"]
        url = stream["url"]

        if url in seen:
            continue

        seen.add(url)

        lines.append(
            make_extinf(
                entry["match"],
                entry["blv"],
                stream,
                entry["api"]
            )
        )

        lines.extend(make_header_lines(stream))
        lines.append(url)

    return "\n".join(lines) + "\n"


# ============================================================
# 18. KEEP OLD PLAYLIST ENTRIES
# ============================================================

def read_old_playlist_blocks(path):
    """
    Đọc các block EXTINF cũ để có thể giữ lại
    dữ liệu nguồn bị lỗi trong playlist tổng.
    """

    path = Path(path)

    if not path.exists():
        return []

    try:
        content = path.read_text(
            encoding="utf-8",
            errors="replace"
        )
    except Exception:
        return []

    lines = content.splitlines()

    blocks = []
    current = []

    for line in lines:

        if line.startswith("#EXTINF:"):

            if current:
                blocks.append(current)

            current = [line]

        elif current:
            current.append(line)

    if current:
        blocks.append(current)

    return blocks


def merge_old_source_playlists(entries, failed_apis):
    """
    Trả về playlist tổng dạng text.
    Nguồn lỗi được lấy block cũ từ playlist riêng.
    """

    fresh = generate_m3u(entries)
    lines = fresh.splitlines()

    seen_urls = set()

    for line in lines:
        if line.startswith("http://") or line.startswith("https://"):
            seen_urls.add(line.strip())

    for api in failed_apis:

        playlist_name = api.get(
            "playlist_file",
            f"{source_id(api)}.m3u"
        )

        old_path = ROOT / playlist_name

        blocks = read_old_playlist_blocks(old_path)

        for block in blocks:

            url = ""

            for line in reversed(block):
                if line.startswith(("http://", "https://")):
                    url = line.strip()
                    break

            if not url or url in seen_urls:
                continue

            lines.extend(block)
            seen_urls.add(url)

            log.info(
                "Giữ lại luồng cũ của nguồn %s",
                source_name(api)
            )

    return "\n".join(lines) + "\n"


# ============================================================
# 19. EPG GENERATOR
# ============================================================

def generate_epg(entries):
    root = ET.Element(
        "tv",
        {
            "generator-info-name": "MHD Sports"
        }
    )

    seen = set()

    for entry in entries:

        match = entry["match"]
        tvg_id = match_tvg_id(match)

        if tvg_id in seen:
            continue

        seen.add(tvg_id)

        dt = parse_datetime(match.get("matchTime"))

        if not dt:
            continue

        title = match_title(match)

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

        programme = ET.SubElement(
            root,
            "programme",
            {
                "start": dt.astimezone(VN_TZ).strftime(
                    "%Y%m%d%H%M%S +0700"
                ),
                "channel": tvg_id
            }
        )

        ET.SubElement(
            programme,
            "title",
            {"lang": "vi"}
        ).text = title

        league = clean(
            match.get("league", {}).get("name")
        )

        if league:
            ET.SubElement(
                programme,
                "sub-title",
                {"lang": "vi"}
            ).text = league

        ET.SubElement(
            programme,
            "category",
            {"lang": "vi"}
        ).text = match_sport(match)

        if match.get("score"):
            ET.SubElement(
                programme,
                "desc",
                {"lang": "vi"}
            ).text = "Tỉ số: " + clean(match["score"])

    return ET.tostring(
        root,
        encoding="unicode",
        xml_declaration=True
    )


# ============================================================
# 20. SOURCES.JSON
# ============================================================

def build_sources(api_results, api_errors):
    result = {
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "sources": []
    }

    for api in CONFIG.get("apis", []):

        sid = source_id(api)
        matches = api_results.get(sid)

        source = {
            "id": sid,
            "name": api.get("name", sid),
            "display_name": api.get(
                "display_name",
                api.get("name", sid)
            ),
            "logo": api.get("logo", ""),
            "playlist": api.get(
                "playlist_file",
                f"{sid}.m3u"
            ),
            "ok": matches is not None,
            "error": api_errors.get(sid, ""),
            "leagues": []
        }

        if matches is None:
            result["sources"].append(source)
            continue

        groups = {}

        for match in matches:

            if not any(
                blv.get("streams")
                for blv in match.get("blvs", [])
            ):
                continue

            league_name = clean(
                match.get("league", {}).get("name")
            ) or "Thể thao"

            if league_name not in groups:
                groups[league_name] = {
                    "name": league_name,
                    "logo": match.get(
                        "league", {}
                    ).get("logo", ""),
                    "matches": []
                }

            teams = match["teams"]
            dt = parse_datetime(match.get("matchTime"))

            stream_count = sum(
                len(blv.get("streams", []))
                for blv in match.get("blvs", [])
            )

            groups[league_name]["matches"].append({
                "id": match_tvg_id(match),
                "title": match_title(match),
                "home": teams["home"]["name"],
                "away": teams["away"]["name"],
                "home_logo": teams["home"]["logo"],
                "away_logo": teams["away"]["logo"],
                "time": dt.isoformat() if dt else "",
                "time_label": (
                    dt.astimezone(VN_TZ).strftime("%d/%m %H:%M")
                    if dt else ""
                ),
                "status": match.get("status", ""),
                "score": match.get("score", ""),
                "stream_count": stream_count
            })

        source["leagues"] = list(groups.values())

        source["match_count"] = sum(
            len(group["matches"])
            for group in source["leagues"]
        )

        source["stream_count"] = sum(
            item["stream_count"]
            for group in source["leagues"]
            for item in group["matches"]
        )

        result["sources"].append(source)

    return result


# ============================================================
# 21. MAIN UPDATE
# ============================================================

def main():
    apis = [
        api
        for api in CONFIG.get("apis", [])
        if api.get("enabled", True)
    ]

    if not apis:
        log.error("Không có API nào được bật.")
        return 1

    api_results = {}
    api_errors = {}

    all_entries = []
    failed_apis = []

    success_count = 0
    failed_count = 0

    # --------------------------------------------------------
    # Fetch và parse từng API.
    # --------------------------------------------------------

    for api in apis:

        name = source_name(api)
        sid = source_id(api)

        log.info("")
        log.info("================================")
        log.info("Đang cập nhật: %s", name)
        log.info("Parser: %s", get_parser(api))
        log.info("================================")

        try:
            raw_items = fetch_api(api)

            log.info(
                "%s: API trả về %s bản ghi",
                name,
                len(raw_items)
            )

            matches = parse_api_items(
                raw_items,
                api
            )

            attach_api_referers(matches, api)

            entries = collect_entries(
                matches,
                api
            )

            api_results[sid] = matches
            all_entries.extend(entries)

            success_count += 1

            log.info(
                "%s: %s trận, %s luồng",
                name,
                len(matches),
                len(entries)
            )

        except Exception as exc:

            failed_count += 1

            api_results[sid] = None
            api_errors[sid] = str(exc)

            failed_apis.append(api)

            log.error(
                "%s: API lỗi: %s",
                name,
                exc
            )

    # --------------------------------------------------------
    # Không ghi đè playlist tổng nếu tất cả API lỗi.
    # --------------------------------------------------------

    if success_count == 0:

        log.error(
            "Tất cả API đều lỗi. Giữ nguyên dữ liệu cũ."
        )

        return 1

    # --------------------------------------------------------
    # Playlist tổng.
    # --------------------------------------------------------

    if all_entries or failed_apis:

        global_m3u = merge_old_source_playlists(
            all_entries,
            failed_apis
        )

        if len(global_m3u.splitlines()) > 1:

            write_file(
                OUTPUT_FILE,
                global_m3u
            )

            log.info(
                "Đã tạo playlist tổng: %s",
                OUTPUT_FILE
            )

        else:
            log.warning(
                "Không có luồng hợp lệ. Không ghi playlist tổng."
            )

    # --------------------------------------------------------
    # Playlist riêng từng API.
    # --------------------------------------------------------

    for api in apis:

        sid = source_id(api)
        matches = api_results.get(sid)

        # API lỗi: giữ playlist cũ.
        if matches is None:
            log.warning(
                "%s lỗi: giữ playlist riêng cũ.",
                sid
            )
            continue

        entries = collect_entries(
            matches,
            api
        )

        if not entries:
            log.warning(
                "%s không có luồng. Giữ playlist riêng cũ.",
                sid
            )
            continue

        playlist_name = api.get(
            "playlist_file",
            f"{sid}.m3u"
        )

        playlist_path = ROOT / playlist_name

        write_file(
            playlist_path,
            generate_m3u(entries)
        )

        log.info(
            "Đã tạo playlist riêng: %s",
            playlist_path
        )

    # --------------------------------------------------------
    # EPG.
    # --------------------------------------------------------

    if all_entries:

        epg_content = generate_epg(all_entries)

        write_file(
            EPG_FILE,
            epg_content
        )

        log.info(
            "Đã tạo EPG: %s",
            EPG_FILE
        )

    # --------------------------------------------------------
    # sources.json.
    # --------------------------------------------------------

    sources = build_sources(
        api_results,
        api_errors
    )

    write_json(
        SOURCES_FILE,
        sources
    )

    log.info(
        "Đã cập nhật sources.json"
    )

    # --------------------------------------------------------
    # Summary.
    # --------------------------------------------------------

    log.info("")
    log.info("--------------------------------")
    log.info("CẬP NHẬT HOÀN TẤT")
    log.info("API thành công: %s", success_count)
    log.info("API lỗi: %s", failed_count)
    log.info("Tổng luồng mới: %s", len(all_entries))
    log.info("--------------------------------")

    return 0


# ============================================================
# 22. RUN
# ============================================================

if __name__ == "__main__":

    try:
        raise SystemExit(main())

    except KeyboardInterrupt:
        log.warning("Đã dừng chương trình.")
        raise SystemExit(130)

    except Exception:
        log.exception("Lỗi không xác định.")
        raise SystemExit(1)
