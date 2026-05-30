import argparse
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE = "https://ekitan.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}
CACHE_SCHEMA_VERSION = 4
ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / "data" / "cache"
RAW_CACHE_PATH = CACHE_DIR / "timetable.json"

MONORAIL_STOP_TIMES_PATH = CACHE_DIR / "osaka_monorail_stop_times.json"
MONORAIL_TO_KADOMA_PATH = CACHE_DIR / "osaka_monorail_to_kadoma.json"
KEIHAN_NEYAGAWA_TO_KADOMA_PATH = CACHE_DIR / "keihan_neyagawa_to_kadoma.json"
KEIHAN_KADOMA_TO_NEYAGAWA_PATH = CACHE_DIR / "keihan_kadoma_to_neyagawa.json"

_CACHE = None
logger = logging.getLogger(__name__)

MONO_LINE = "322"
MONO_STATIONS = [
    "大阪空港",
    "蛍池",
    "柴原阪大前",
    "少路",
    "千里中央",
    "山田",
    "万博記念公園",
    "宇野辺",
    "南茨木",
    "沢良宜",
    "摂津",
    "南摂津",
    "大日",
    "門真市",
]
MONO_KADOMA_IDX = 13

KEIHAN_LINE = "366"
KEIHAN_KADOMA_IDX = 12
KEIHAN_KAYASHIMA = "萱島"
KEIHAN_NEYAGAWA = "寝屋川市"
KEIHAN_NEYAGAWA_IDX = 16
KEIHAN_TRANSFER_TYPES_TO_KADOMA = {"普通", "区急", "区間急行"}
KEIHAN_TRANSFER_TYPES_TO_NEYAGAWA = {"準急"}


def _empty_cache():
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "fetched_at": None,
        "departures": {},
        "train_station_times": {},
    }


def _load_cache():
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    cache = _empty_cache()
    if RAW_CACHE_PATH.exists() and RAW_CACHE_PATH.stat().st_size > 0:
        try:
            with RAW_CACHE_PATH.open(encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("schema_version") == CACHE_SCHEMA_VERSION:
                cache.update(
                    {
                        "fetched_at": saved.get("fetched_at"),
                        "departures": saved.get("departures", {}),
                        "train_station_times": saved.get("train_station_times", {}),
                    }
                )
        except (OSError, json.JSONDecodeError):
            logger.warning("cache load failed; starting with empty cache: %s", RAW_CACHE_PATH)

    _CACHE = cache
    return _CACHE


def _save_cache():
    cache = _load_cache()
    cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = RAW_CACHE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, RAW_CACHE_PATH)


def _write_json(path, payload):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def _timetable_cache_key(url):
    parsed = urlparse(url)
    m = re.search(r"line-station/([^/?]+)/d(\d+)", parsed.path)
    if not m:
        raise ValueError(f"URLパターンが不正: {url}")
    dt = parse_qs(parsed.query).get("dt", [""])[0]
    suffix = f"-dt{dt}" if dt else ""
    return f"{m.group(1)}-d{m.group(2)}{suffix}"


def url_for(line, station_idx, direction, service_date=None):
    url = f"{BASE}/timetable/railway/line-station/{line}-{station_idx}/{direction}"
    if service_date:
        url = f"{url}?{urlencode({'dt': service_date})}"
    return url


def _monorail_source_for_kadoma(station):
    station_idx = MONO_STATIONS.index(station)
    direction = "d1" if station == "大阪空港" else "d2"
    return station_idx, direction


def _next_date_for_weekday(target_weekday):
    today = date.today()
    days = (target_weekday - today.weekday()) % 7
    return (today + timedelta(days=days)).strftime("%Y%m%d")


def default_service_date(service_day):
    if service_day == "weekday":
        return _next_date_for_weekday(0)
    if service_day == "weekend":
        return _next_date_for_weekday(5)
    raise ValueError(f"未知の運行日種別: {service_day}")


def _path_for_service_day(path, service_day):
    return path.with_name(f"{path.stem}_{service_day}{path.suffix}")


def minutes_to_time(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_train_type(text):
    m = re.search(r"\[(.+?)\]", text)
    return m.group(1) if m else "普通"


def _parse_destination(text):
    text = re.sub(r"\s+", " ", text).strip()
    matches = re.findall(r"([^\s\[\]（）()]+)行", text)
    return matches[-1] if matches else None


def fetch_departures(url):
    cache = _load_cache()
    cache_key = _timetable_cache_key(url)
    cached = cache["departures"].get(cache_key)
    if cached:
        logger.debug("cache hit: departures %s", cache_key)
        return cached["trains"]

    m = re.search(r"line-station/([^/?]+)/d(\d+)", url)
    if not m:
        raise ValueError(f"URLパターンが不正: {url}")
    expected_sff = m.group(1)
    expected_d = m.group(2)

    logger.info("cache miss: fetching departures %s", cache_key)
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/timetable/railway/train" not in href:
            continue
        q = parse_qs(urlparse(href).query)
        if q.get("SFF", [""])[0] != expected_sff:
            continue
        if q.get("d", [""])[0] != expected_d:
            continue
        dep = q.get("departure", [""])[0]
        if not dep.isdigit() or len(dep) != 4:
            continue

        text = a.get_text(separator=" ", strip=True)
        hour = int(dep[:2])
        minute = int(dep[2:])
        out.append(
            {
                "dep": hour * 60 + minute,
                "time": f"{hour:02d}:{minute:02d}",
                "hour": hour,
                "minute": minute,
                "type": _parse_train_type(text),
                "destination": _parse_destination(text),
                "label": text,
                "url": urljoin(BASE, href),
            }
        )

    if out:
        cache["departures"][cache_key] = {
            "url": url,
            "line_station": expected_sff,
            "direction": f"d{expected_d}",
            "trains": out,
        }
        _save_cache()
        logger.info("cached departures %s (%d trains)", cache_key, len(out))
    return out


def _normalize_station_name(name):
    name = name.strip()
    name = name.removeprefix("[")
    name = name.removesuffix("]")
    name = re.sub(r"\(.+?\)$", "", name)
    name = name.removesuffix("駅")
    return name


def _fetch_train_station_times(detail_url):
    logger.info("cache miss: fetching train station times")
    r = requests.get(detail_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    station_times = {}
    for td in soup.find_all("td", class_="td-station-name"):
        name = _normalize_station_name(td.get_text(strip=True))
        tr = td.find_parent("tr")
        if tr is None:
            continue
        times = re.findall(r"(\d{1,2}):(\d{2})", tr.get_text(" ", strip=True))
        if not times:
            continue
        h, m = times[-1]
        station_times[name] = int(h) * 60 + int(m)
    return station_times


def _train_station_times(detail_url):
    cache = _load_cache()
    station_times = cache["train_station_times"].get(detail_url)
    if station_times is None:
        station_times = _fetch_train_station_times(detail_url)
        cache["train_station_times"][detail_url] = station_times
        _save_cache()
        logger.info("cached train station times (%d stations)", len(station_times))
    else:
        logger.debug("cache hit: train station times")
    return station_times


def find_station_time(detail_url, station_name, base_minutes=None):
    station_times = _train_station_times(detail_url)
    t = station_times.get(_normalize_station_name(station_name))
    if t is None:
        return None
    if base_minutes is None:
        return t
    return t if t >= base_minutes else None


def _station_time_for_train(train, station_name):
    return find_station_time(train["url"], station_name, base_minutes=train["dep"])


def _train_summary(train):
    return {
        "departure": train["time"],
        "type": train["type"],
        "destination": train.get("destination"),
        "label": train.get("label"),
        "url": train["url"],
    }


def _with_time_fields(payload, *time_fields):
    for key, minutes in time_fields:
        payload[key] = minutes_to_time(minutes) if minutes is not None else None
    return payload


def _next_train_after(trains, ready_minutes, allowed_types=None, station_name=None):
    for train in sorted(trains, key=lambda x: x["dep"]):
        if train["dep"] < ready_minutes:
            continue
        if allowed_types is not None and train["type"] not in allowed_types:
            continue
        if station_name is not None and _station_time_for_train(train, station_name) is None:
            continue
        return train
    return None


def _monorail_pattern_key(train):
    return (train["type"], train.get("destination") or "")


def _monorail_offsets_for_pattern(train):
    station_times = _train_station_times(train["url"])
    return {
        station: minutes - train["dep"]
        for station, minutes in station_times.items()
        if minutes >= train["dep"]
    }


def build_monorail_stop_times(service_day, service_date):
    """門真市からの大阪モノレール各列車について、各駅への停車時刻をまとめる。"""
    source_url = url_for(MONO_LINE, MONO_KADOMA_IDX, "d1", service_date)
    trains = fetch_departures(source_url)
    rows = []
    target_stations = list(reversed(MONO_STATIONS[:-1]))
    pattern_offsets = {}

    for train in trains:
        pattern_key = _monorail_pattern_key(train)
        if pattern_key not in pattern_offsets:
            pattern_offsets[pattern_key] = _monorail_offsets_for_pattern(train)
        offsets = pattern_offsets[pattern_key]

        stops = {}
        for station in target_stations:
            offset = offsets.get(station)
            t = train["dep"] + offset if offset is not None else None
            stops[station] = minutes_to_time(t) if t is not None else None
        row = _train_summary(train)
        row["stops"] = stops
        rows.append(row)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_day": service_day,
        "service_date": service_date,
        "line": "大阪モノレール",
        "direction": "門真市発",
        "source_url": source_url,
        "stations": target_stations,
        "trains": rows,
    }
    _write_json(_path_for_service_day(MONORAIL_STOP_TIMES_PATH, service_day), payload)
    return payload


def _load_json(path, default):
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("json load failed; starting fresh: %s", path)
        return default


def _empty_monorail_to_kadoma_payload(service_day, service_date):
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_day": service_day,
        "service_date": service_date,
        "line": "大阪モノレール",
        "direction": "門真市方面",
        "stations": list(MONO_STATIONS[:-1]),
        "by_station": {},
    }
    return payload


def _build_monorail_to_kadoma_station_rows(station, service_date):
    if station not in MONO_STATIONS:
        raise ValueError(f"未知の駅名: {station}")
    if station == "門真市":
        raise ValueError("門真市駅は到着駅なので指定できません")

    station_idx, direction = _monorail_source_for_kadoma(station)
    source_url = url_for(MONO_LINE, station_idx, direction, service_date)
    trains = fetch_departures(source_url)
    rows = []
    pattern_offsets = {}

    for train in trains:
        pattern_key = _monorail_pattern_key(train)
        if pattern_key not in pattern_offsets:
            pattern_offsets[pattern_key] = _monorail_offsets_for_pattern(train)
        offset = pattern_offsets[pattern_key].get("門真市")
        kadoma_arrival = train["dep"] + offset if offset is not None else None
        row = _train_summary(train)
        _with_time_fields(row, ("kadoma_arrival", kadoma_arrival))
        rows.append(row)

    return source_url, rows


def build_monorail_to_kadoma_station(station, service_day, service_date):
    """指定駅から門真市方面へ向かう列車の門真市到着時刻だけを追記生成する。"""
    source_url, rows = _build_monorail_to_kadoma_station_rows(station, service_date)
    output_path = _path_for_service_day(MONORAIL_TO_KADOMA_PATH, service_day)
    payload = _load_json(output_path, _empty_monorail_to_kadoma_payload(service_day, service_date))
    payload.update(
        {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "service_day": service_day,
            "service_date": service_date,
            "line": "大阪モノレール",
            "direction": "門真市方面",
            "stations": list(MONO_STATIONS[:-1]),
        }
    )
    payload.setdefault("by_station", {})[station] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_url": source_url,
        "trains": rows,
    }
    _write_json(output_path, payload)
    return payload


def build_monorail_to_kadoma_all_stations(service_day, service_date):
    """門真市以外の全モノレール駅から門真市方面へのキャッシュを生成する。"""
    payload = _empty_monorail_to_kadoma_payload(service_day, service_date)
    for station in MONO_STATIONS[:-1]:
        source_url, rows = _build_monorail_to_kadoma_station_rows(station, service_date)
        payload["by_station"][station] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
            "trains": rows,
        }

    output_path = _path_for_service_day(MONORAIL_TO_KADOMA_PATH, service_day)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(output_path, payload)
    return payload


def build_keihan_neyagawa_to_kadoma(service_day, service_date):
    """寝屋川市から門真市方面。準急は萱島で普通/区間急行に接続して門真市へ向かう。"""
    source_url = url_for(KEIHAN_LINE, KEIHAN_NEYAGAWA_IDX, "d1", service_date)
    transfer_source_url = url_for(KEIHAN_LINE, 15, "d1", service_date)
    neya_trains = fetch_departures(source_url)
    kayashima_trains = fetch_departures(transfer_source_url)
    rows = []

    for train in neya_trains:
        if train["type"] == "普通":
            kadoma_arrival = _station_time_for_train(train, "門真市")
            if kadoma_arrival is None:
                continue
            row = _train_summary(train)
            row["route"] = "direct"
            _with_time_fields(row, ("kadoma_arrival", kadoma_arrival))
            rows.append(row)
            continue

        if train["type"] != "準急":
            continue

        kayashima_arrival = _station_time_for_train(train, KEIHAN_KAYASHIMA)
        if kayashima_arrival is None:
            continue
        transfer = _next_train_after(
            kayashima_trains,
            kayashima_arrival,
            allowed_types=KEIHAN_TRANSFER_TYPES_TO_KADOMA,
            station_name="門真市",
        )
        row = _train_summary(train)
        row["route"] = "transfer_at_kayashima"
        _with_time_fields(row, ("kayashima_arrival", kayashima_arrival))
        if transfer is not None:
            kadoma_arrival = _station_time_for_train(transfer, "門真市")
            row["transfer_train"] = _train_summary(transfer)
            _with_time_fields(
                row["transfer_train"],
                ("kayashima_departure", transfer["dep"]),
                ("kadoma_arrival", kadoma_arrival),
            )
        else:
            row["transfer_train"] = None
        rows.append(row)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_day": service_day,
        "service_date": service_date,
        "line": "京阪電車",
        "direction": "寝屋川市発 門真市方面",
        "source_url": source_url,
        "transfer_source_url": transfer_source_url,
        "trains": rows,
    }
    _write_json(_path_for_service_day(KEIHAN_NEYAGAWA_TO_KADOMA_PATH, service_day), payload)
    return payload


def build_keihan_kadoma_to_neyagawa(service_day, service_date):
    """門真市から寝屋川市方面。萱島止まりは次の準急で寝屋川市へ向かう。"""
    source_url = url_for(KEIHAN_LINE, KEIHAN_KADOMA_IDX, "d2", service_date)
    transfer_source_url = url_for(KEIHAN_LINE, 15, "d2", service_date)
    kadoma_trains = fetch_departures(source_url)
    kayashima_trains = fetch_departures(transfer_source_url)
    rows = []

    for train in kadoma_trains:
        row = _train_summary(train)
        neyagawa_arrival = _station_time_for_train(train, KEIHAN_NEYAGAWA)
        if neyagawa_arrival is not None:
            row["route"] = "direct"
            _with_time_fields(row, ("neyagawa_arrival", neyagawa_arrival))
            rows.append(row)
            continue

        kayashima_arrival = _station_time_for_train(train, KEIHAN_KAYASHIMA)
        if kayashima_arrival is None:
            continue

        transfer = _next_train_after(
            kayashima_trains,
            kayashima_arrival,
            allowed_types=KEIHAN_TRANSFER_TYPES_TO_NEYAGAWA,
            station_name=KEIHAN_NEYAGAWA,
        )
        row["route"] = "transfer_at_kayashima"
        _with_time_fields(row, ("kayashima_arrival", kayashima_arrival))
        if transfer is not None:
            transfer_arrival = _station_time_for_train(transfer, KEIHAN_NEYAGAWA)
            row["transfer_train"] = _train_summary(transfer)
            _with_time_fields(
                row["transfer_train"],
                ("kayashima_departure", transfer["dep"]),
                ("neyagawa_arrival", transfer_arrival),
            )
        else:
            row["transfer_train"] = None
        rows.append(row)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_day": service_day,
        "service_date": service_date,
        "line": "京阪電車",
        "direction": "門真市発 寝屋川市方面",
        "source_url": source_url,
        "transfer_source_url": transfer_source_url,
        "trains": rows,
    }
    _write_json(_path_for_service_day(KEIHAN_KADOMA_TO_NEYAGAWA_PATH, service_day), payload)
    return payload


def build_all_caches(service_day, service_date):
    builders = [
        (MONORAIL_STOP_TIMES_PATH, build_monorail_stop_times),
        (MONORAIL_TO_KADOMA_PATH, build_monorail_to_kadoma_all_stations),
        (KEIHAN_NEYAGAWA_TO_KADOMA_PATH, build_keihan_neyagawa_to_kadoma),
        (KEIHAN_KADOMA_TO_NEYAGAWA_PATH, build_keihan_kadoma_to_neyagawa),
    ]
    outputs = []
    for path, builder in builders:
        payload = builder(service_day, service_date)
        output_path = _path_for_service_day(path, service_day)
        outputs.append({"path": str(output_path), "count": _payload_count(payload)})
        logger.info("wrote %s", output_path)
    return outputs


def _payload_count(payload):
    if "trains" in payload:
        return len(payload["trains"])
    if "by_station" in payload:
        return sum(len(v["trains"]) for v in payload["by_station"].values())
    return 0


def _selected_service_days(args):
    dates = {
        "weekday": args.weekday_date or default_service_date("weekday"),
        "weekend": args.weekend_date or default_service_date("weekend"),
    }
    if args.service_day == "both":
        return [("weekday", dates["weekday"]), ("weekend", dates["weekend"])]
    return [(args.service_day, dates[args.service_day])]


def main():
    parser = argparse.ArgumentParser(description="駅探から乗換用時刻表JSONを data/cache に生成します")
    parser.add_argument(
        "--only",
        choices=["all", "mono-stops", "mono-to-kadoma", "keihan-to-kadoma", "keihan-to-neyagawa"],
        default="all",
    )
    parser.add_argument(
        "--service-day",
        choices=["weekday", "weekend", "both"],
        default="both",
        help="保存する運行日種別。weekday は平日、weekend は土日ダイヤ用です",
    )
    parser.add_argument("--weekday-date", help="平日ダイヤ取得に使う日付 YYYYMMDD。未指定なら次の月曜")
    parser.add_argument("--weekend-date", help="土日ダイヤ取得に使う日付 YYYYMMDD。未指定なら次の土曜")
    parser.add_argument(
        "--mono-station",
        help="--only mono-to-kadoma のときに生成する大阪モノレール駅名。未指定なら全駅分を生成します",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    outputs = []
    for service_day, service_date in _selected_service_days(args):
        logger.info("building service_day=%s service_date=%s", service_day, service_date)
        if args.only == "all":
            outputs.extend(build_all_caches(service_day, service_date))
        elif args.only == "mono-stops":
            payload = build_monorail_stop_times(service_day, service_date)
            output_path = _path_for_service_day(MONORAIL_STOP_TIMES_PATH, service_day)
            outputs.append({"path": str(output_path), "count": _payload_count(payload)})
        elif args.only == "mono-to-kadoma":
            if args.mono_station:
                payload = build_monorail_to_kadoma_station(args.mono_station, service_day, service_date)
                count = len(payload["by_station"][args.mono_station]["trains"])
            else:
                payload = build_monorail_to_kadoma_all_stations(service_day, service_date)
                count = _payload_count(payload)
            output_path = _path_for_service_day(MONORAIL_TO_KADOMA_PATH, service_day)
            outputs.append({"path": str(output_path), "count": count})
        elif args.only == "keihan-to-kadoma":
            payload = build_keihan_neyagawa_to_kadoma(service_day, service_date)
            output_path = _path_for_service_day(KEIHAN_NEYAGAWA_TO_KADOMA_PATH, service_day)
            outputs.append({"path": str(output_path), "count": _payload_count(payload)})
        else:
            payload = build_keihan_kadoma_to_neyagawa(service_day, service_date)
            output_path = _path_for_service_day(KEIHAN_KADOMA_TO_NEYAGAWA_PATH, service_day)
            outputs.append({"path": str(output_path), "count": _payload_count(payload)})

    for output in outputs:
        print(f"{output['path']} ({output['count']} records)")


if __name__ == "__main__":
    main()
