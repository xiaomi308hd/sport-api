
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
    if not value:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                value / 1000 if value > 9999999999 else value,
                tz=timezone.utc
            )
        except Exception:
            return None

    value = clean(value)

    try:
        result = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result

    except Exception:
        pass

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).replace(
                tzinfo=timezone.utc
            )
        except Exception:
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
                timeout=timeout or TIMEOUT
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
        "matchTime": clean(
            first_value(
                item.get("startTime"),
                item.get("matchTime"),
                item.get("date")
            )
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

def fetch_api(api):
    parser = clean(
        api.get("parser") or api.get("api_type")
    ).lower()

    if parser in ("plapi", "plapi_graph"):
        return fetch_plapi(api)

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

    for item in items:
        try:
            if parser in ("plapi", "plapi_graph"):
                match = parse_plapi_match(item, api)

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
        'group-title="{}"'.format(m3u_escape(league)),
        'x-source="{}"'.format(
            m3u_escape(source.get("id") or broadcaster)
        )
    ]

    if source.get("name"):
        attributes.append(
            'x-source-name="{}"'.format(m3u_escape(source["name"]))
        )

    source_logo = first_value(source.get("logo"), api_logo)

    if source_logo:
        attributes.append(
            'x-source-logo="{}"'.format(m3u_escape(source_logo))
        )

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
                    "logo": clean(api.get("logo"))
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

    # Global playlist.
    if failed_count == 0:
        global_m3u = generate_m3u(all_entries)

        atomic_write(
            OUTPUT_FILE,
            global_m3u
        )

        log.info(
            "Đã tạo %s (%s luồng)",
            OUTPUT_FILE.name,
            len(all_entries)
        )
    else:
        log.warning(
            "Có API lỗi (%s/%s). "
            "Giữ nguyên playlist tổng hiện tại: %s",
            failed_count,
            len(apis),
            OUTPUT_FILE.name
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
    if failed_count == 0:
        epg_content = generate_epg(all_entries)

        atomic_write(
            EPG_FILE,
            epg_content
        )

        log.info("Đã tạo %s", EPG_FILE.name)
    else:
        log.warning(
            "Có API lỗi. Giữ nguyên EPG hiện tại: %s",
            EPG_FILE.name
        )

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
