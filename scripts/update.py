
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MHD SPORTS - API UPDATE
- Hỗ trợ nhiều API nguồn thể thao.
- Parser riêng: CoLa TV, Gà Vàng TV, Chuỗi Chiến TV, PLAPI.
- Tạo playlist M3U tổng và playlist riêng từng nguồn.
- Tạo EPG XML và sources.json.
- Hỗ trợ Referer, Origin, User-Agent và headers riêng.
- Khi một API lỗi, giữ lại playlist cũ của API đó.
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
        raise FileNotFoundError("Không tìm thấy config.json")

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


# ============================================================
# 3. HTTP REQUEST
# ============================================================

def request_json(
    url,
    method="GET",
    headers=None,
    params=None,
    body=None
):
    method = clean(method).upper() or "GET"
    headers = dict(headers or {})

    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/157.0.0.0 Safari/537.36"
    )

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

                return json.loads(raw) if raw.strip() else None

        except urllib.error.HTTPError as exc:
            if exc.code in (408, 425, 429, 500, 502, 503, 504):
                if attempt < RETRIES:
                    time.sleep(min(2 ** attempt, 10))
                    continue

            raise RuntimeError(
                f"HTTP {exc.code}: {exc.reason}"
            )

        except Exception as exc:
            if attempt < RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue

            raise RuntimeError(str(exc))

    raise RuntimeError("Không thể kết nối API")


# ============================================================
# 4. EXTRACT API DATA
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

        # CoLa TV trả data dạng object:
        # data = {"match_id_1": {...}, "match_id_2": {...}}
        return [
            value
            for value in data.values()
            if isinstance(value, dict)
        ]

    return []


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
            stream.get("playUrl")
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


# ============================================================
# 6. GENERIC PARSER
# ============================================================

def parse_generic_match(item, api):
    if not isinstance(item, dict):
        return None

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

    referer = api.get("default_stream_referer", "")

    streams = []

    for key in (
        "streams",
        "streamUrls",
        "sources",
        "links"
    ):
        values = item.get(key)

        if isinstance(values, list):
            for value in values:
                stream = normalize_stream(value, referer)

                if stream:
                    streams.append(stream)

    for key in ("url", "link", "source_live"):
        value = item.get(key)

        if isinstance(value, str):
            stream = normalize_stream(value, referer)

            if stream:
                streams.append(stream)

    streams = unique_streams(streams)

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

        "sport": clean(first_value(
            item.get("sport"),
            item.get("sportName")
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
            "name": api.get("display_name", api.get("name", "API")),
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
        "Thể thao"
    )

    if isinstance(league, dict):
        league = league.get("name", "Thể thao")

    return {
        "externalId": clean(item.get("id")),

        "matchTime": clean(first_value(
            item.get("startTime"),
            item.get("matchTime"),
            item.get("date")
        )),

        "status": clean(item.get("status")),

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
            "name": api.get("display_name", "Gà Vàng TV"),
            "streams": unique_streams(streams)
        }] if streams else []
    }


# ============================================================
# 8. COLA TV PARSER
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

    # Luồng chính của trận.
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

    # Danh sách bình luận viên.
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

            # HLS chính của BLV.
            add_stream(
                streams,
                appointment.get("playStreamAddress2"),
                "HLS"
            )

            # Các server HLS dự phòng.
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

            # FLV của BLV.
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
# 9. CHUOI CHIEN TV PARSER
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
        ["blvs", "blvs_bonglau", "blvs_nguoitho"]
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

    # Nếu không có nhóm BLV, thử đọc luồng trực tiếp.
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
# 10. PLAPI PARSER
# ============================================================

def parse_plapi_match(item, api):
    if not isinstance(item, dict):
        return None

    status = clean(first_value(
        item.get("status"),
        item.get("match_status")
    )).lower()

    if status in ("finished", "ended", "completed", "ft"):
        return None

    home = first_value(
        item.get("team_1"),
        item.get("home"),
        "Đội nhà"
    )

    away = first_value(
        item.get("team_2"),
        item.get("away"),
        "Đội khách"
    )

    league = first_value(
        item.get("league"),
        "Thể thao"
    )

    if isinstance(league, dict):
        league = league.get("name", "Thể thao")

    referer = api.get("default_stream_referer", "")

    streams = []

    source_live = item.get("source_live")

    if isinstance(source_live, str):
        stream = normalize_stream(source_live, referer)

        if stream:
            streams.append(stream)

    # Các trường luồng có thể được API trả trực tiếp.
    for key in ("hd_1", "hd_2", "sd_1", "sd_2", "source"):
        value = item.get(key)

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
                    "label": key.upper()
                },
                referer
            )

            if stream:
                streams.append(stream)

    streams = unique_streams(streams)

    return {
        "externalId": clean(item.get("id")),

        "matchTime": clean(first_value(
            item.get("start_date"),
            item.get("matchTime"),
            item.get("date")
        )),

        "status": clean(status),

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
            "streams": streams
        }] if streams else []
    }


# ============================================================
# 11. FETCH API
# ============================================================

def fetch_api(api):
    parser = clean(api.get("parser")).lower()
    source_id = clean(api.get("id")).lower()

    # PLAPI có thể sử dụng POST.
    payload = request_json(
        api["url"],
        method=api.get("method", "GET"),
        headers=api.get("headers", {}),
        params=api.get("params"),
        body=api.get("body")
    )

    return unwrap_data(payload)


# ============================================================
# 12. PARSER DISPATCH
# ============================================================

def parse_api_items(items, api):
    parser = clean(api.get("parser")).lower()
    source_id = clean(api.get("id")).lower()

    result = []

    for item in items:

        try:
            if parser == "colatv" or source_id == "colatv":
                match = parse_colatv_match(item, api)

            elif parser == "gavangtv" or source_id == "gavangtv":
                match = parse_gavang_match(item, api)

            elif parser == "chuoichientv" or source_id == "chuoichientv":
                match = parse_chuoichien_match(item, api)

            elif parser in ("plapi", "plapi_graph"):
                match = parse_plapi_match(item, api)

            else:
                match = parse_generic_match(item, api)

            if match:
                result.append(match)

        except Exception as exc:
            log.warning(
                "%s: bỏ qua trận lỗi: %s",
                api.get("name", source_id),
                exc
            )

    return result


# ============================================================
# 13. REFERER
# ============================================================

def attach_api_referers(matches, api):
    common_referer = clean(
        api.get("stream_referer")
        or api.get("default_stream_referer")
    )

    host_rules = api.get("referer_by_host", {})

    for match in matches:
        for blv in match.get("blvs", []):

            for stream in blv.get("streams", []):

                headers = stream.setdefault("headers", {})

                has_referer = any(
                    key.lower() == "referer"
                    for key in headers
                )

                if has_referer:
                    continue

                host = (
                    urllib.parse.urlparse(
                        stream["url"]
                    ).hostname or ""
                ).lower()

                referer = first_value(
                    host_rules.get(host),
                    common_referer
                )

                if referer:
                    headers["Referer"] = referer


# ============================================================
# 14. SPORT DETECTION
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
# 15. M3U GENERATOR
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
        key = clean(key).lower()
        value = clean(value)

        if not value:
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
                "#EXTHTTP:"
                + json.dumps(
                    {key: value},
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
# 16. EPG GENERATOR
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
# 17. SOURCES.JSON
# ============================================================

def build_sources(api_results, api_errors):
    result = {
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "sources": []
    }

    for api in CONFIG.get("apis", []):

        source_id = clean(api.get("id"))
        matches = api_results.get(source_id)

        source = {
            "id": source_id,
            "name": api.get("name", source_id),
            "display_name": api.get(
                "display_name",
                api.get("name", source_id)
            ),
            "logo": api.get("logo", ""),
            "playlist": api.get(
                "playlist_file",
                f"{source_id}.m3u"
            ),
            "ok": matches is not None,
            "error": api_errors.get(source_id, ""),
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
                    "logo": match.get("league", {}).get("logo", ""),
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
# 18. MAIN UPDATE
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

    success_count = 0
    failed_count = 0

    # --------------------------------------------------------
    # Fetch và parse từng API.
    # --------------------------------------------------------

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

            attach_api_referers(matches, api)

            entries = collect_entries(
                matches,
                api
            )

            api_results[source_id] = matches
            all_entries.extend(entries)

            success_count += 1

            log.info(
                "%s: %s bản ghi, %s luồng",
                name,
                len(raw_items),
                len(entries)
            )

        except Exception as exc:

            failed_count += 1

            api_results[source_id] = None
            api_errors[source_id] = str(exc)

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

    if not all_entries:
        log.error(
            "Không có luồng hợp lệ. Không ghi đè playlist."
        )

        write_json(
            SOURCES_FILE,
            build_sources(api_results, api_errors)
        )

        return 1

    # --------------------------------------------------------
    # Playlist tổng.
    # --------------------------------------------------------

    global_m3u = generate_m3u(all_entries)

    write_file(
        OUTPUT_FILE,
        global_m3u
    )

    log.info(
        "Đã tạo playlist tổng: %s",
        OUTPUT_FILE
    )

    # --------------------------------------------------------
    # Playlist riêng từng API.
    # --------------------------------------------------------

    for api in apis:

        source_id = clean(api.get("id"))
        matches = api_results.get(source_id)

        # API lỗi: không ghi đè playlist cũ.
        if matches is None:
            continue

        entries = collect_entries(
            matches,
            api
        )

        if not entries:
            log.warning(
                "%s không có luồng. Bỏ qua playlist riêng.",
                source_id
            )
            continue

        playlist_name = api.get(
            "playlist_file",
            f"{source_id}.m3u"
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

    epg_content = generate_epg(all_entries)

    write_file(
        EPG_FILE,
        epg_content
    )

    log.info("Đã tạo EPG: %s", EPG_FILE)

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

    log.info("--------------------------------")
    log.info("CẬP NHẬT HOÀN TẤT")
    log.info("API thành công: %s", success_count)
    log.info("API lỗi: %s", failed_count)
    log.info("Tổng luồng mới: %s", len(all_entries))
    log.info("--------------------------------")

    return 0


# ============================================================
# 19. RUN
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
