import json
import logging
import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / "data" / "cache"
BUILD_DIR = ROOT_DIR / "build"
CPP_SOURCE = ROOT_DIR / "src" / "route_core.cpp"
CMAKE_FILE = ROOT_DIR / "CMakeLists.txt"
logger = logging.getLogger(__name__)

ROUTE_KINDS = {"fastest_arrival", "shortest_arrival"}
DIRECTIONS = {"to_home", "from_home"}

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

AVAILABLE_MONO_STATIONS = tuple(station for station in MONO_STATIONS if station != "門真市")
SERVICE_DAYS = {"auto", "weekday", "weekend"}
HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _route_core_outputs():
    return list((ROOT_DIR / "src").glob("route_core*.so"))


def _newest_mtime(paths):
    existing = [path.stat().st_mtime for path in paths if path.exists()]
    return max(existing) if existing else 0


def route_core_needs_build():
    outputs = _route_core_outputs()
    if not outputs:
        return True

    output_mtime = _newest_mtime(outputs)
    source_mtime = _newest_mtime([CPP_SOURCE, CMAKE_FILE])
    return source_mtime > output_mtime


def _pybind11_cmake_args():
    try:
        import pybind11
    except ImportError:
        return []
    return [f"-Dpybind11_DIR={pybind11.get_cmake_dir()}"]


def _run_build_command(command):
    logger.info("running: %s", " ".join(command))
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=True,
    )


def ensure_cpp_core_built(force=False, required=False):
    """route_core.cpp を必要なら自動ビルドする。

    app.py 起動時に呼ばれる想定です。ビルド済みで、C++やCMake設定が
    更新されていなければ何もしません。
    """
    if os.environ.get("ROUTE_SKIP_CPP_BUILD") == "1":
        logger.info("C++ auto build skipped by ROUTE_SKIP_CPP_BUILD=1")
        return False

    if not force and not route_core_needs_build():
        logger.info("C++ route_core is already up to date")
        return True

    configure = ["cmake", "-S", str(ROOT_DIR), "-B", str(BUILD_DIR)]
    configure.extend(_pybind11_cmake_args())
    build = ["cmake", "--build", str(BUILD_DIR)]

    try:
        _run_build_command(configure)
        _run_build_command(build)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        message = _build_error_message(exc)
        if required:
            raise RuntimeError(message) from exc
        logger.warning(message)
        return False


def _build_error_message(exc):
    if isinstance(exc, FileNotFoundError):
        return "C++自動ビルドに失敗しました: cmake が見つかりません"

    parts = ["C++自動ビルドに失敗しました"]
    if exc.stdout:
        parts.append(f"stdout:\n{exc.stdout}")
    if exc.stderr:
        parts.append(f"stderr:\n{exc.stderr}")
    return "\n".join(parts)


def minutes_to_hhmm(minutes):
    if minutes is None:
        return None
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _parse_hhmm_parts(text):
    if not isinstance(text, str):
        raise ValueError("時刻は HH:MM 形式で入力してください")
    match = HHMM_RE.fullmatch(text.strip())
    if not match:
        raise ValueError("時刻は HH:MM 形式で入力してください")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if minute > 59:
        raise ValueError("時刻の分は 00 から 59 の範囲で入力してください")
    return hour, minute


def hhmm_to_minutes(text):
    hour, minute = _parse_hhmm_parts(text)
    if hour > 23:
        raise ValueError("時刻の時は 00 から 23 の範囲で入力してください")
    return hour * 60 + minute


def timetable_hhmm_to_minutes(text):
    hour, minute = _parse_hhmm_parts(text)
    if hour > 47:
        raise ValueError("時刻の時は 00 から 47 の範囲で入力してください")
    return hour * 60 + minute


def service_day_for_datetime(dt):
    return "weekend" if dt.weekday() >= 5 else "weekday"


def normalize_service_day(service_day, now=None):
    if service_day not in SERVICE_DAYS:
        raise ValueError("service_day は auto / weekday / weekend のいずれかです")
    if service_day != "auto":
        return service_day
    return service_day_for_datetime(now or datetime.now())


def _validate_request_params(
    kind,
    direction,
    mono_station,
    now_minutes,
    transfer_min,
    home_walk_min,
    school_walk_min,
):
    if kind not in ROUTE_KINDS:
        raise ValueError("kind は fastest_arrival / shortest_arrival のいずれかです")
    if direction not in DIRECTIONS:
        raise ValueError("direction は to_home / from_home のいずれかです")
    if mono_station not in AVAILABLE_MONO_STATIONS:
        raise ValueError("モノレール駅の指定が不正です")

    values = {
        "now_minutes": now_minutes,
        "transfer_min": transfer_min,
        "home_walk_min": home_walk_min,
        "school_walk_min": school_walk_min,
    }
    for name, value in values.items():
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} は0以上の整数で指定してください")


def request(
    kind,
    direction,
    mono_station,
    now_minutes,
    transfer_min,
    home_walk_min,
    school_walk_min,
    service_day="auto",
):
    """C++側へ渡すリクエストを1か所で作る。

    C++側は、まずこの辞書を受け取れるようにするとPython画面とつながります。
    pybind11なら route_core.find_route(request_dict)、実行ファイルなら
    標準入力にこのJSONを流す想定です。
    """
    _validate_request_params(
        kind,
        direction,
        mono_station,
        now_minutes,
        transfer_min,
        home_walk_min,
        school_walk_min,
    )
    resolved_service_day = normalize_service_day(service_day)
    return {
        "kind": kind,
        "direction": direction,
        "mono_station": mono_station,
        "service_day": resolved_service_day,
        "now_minutes": now_minutes,
        "transfer_min": transfer_min,
        "home_walk_min": home_walk_min,
        "school_walk_min": school_walk_min,
        "cache_dir": str(CACHE_DIR),
    }


def find_route(request):
    """C++検索エンジンへ依頼し、画面用の辞書を受け取る。

    優先順位:
      1. pybind11 モジュール route_core.find_route(request)
      2. 環境変数 ROUTE_CORE_COMMAND の外部コマンド
      3. Pythonの簡易フォールバック
    """
    if ensure_cpp_core_built():
        try:
            import route_core

            return route_core.find_route(request)
        except ImportError as exc:
            logger.warning("route_core import failed after build: %s", exc)

    command = os.environ.get("ROUTE_CORE_COMMAND")
    if command:
        return _call_route_core_command(command, request)

    return _fallback_find_route(request)


def _call_route_core_command(command, request):
    completed = subprocess.run(
        shlex.split(command),
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _cache_path(name, service_day):
    return CACHE_DIR / f"{name}_{service_day}.json"


def _require_keys(mapping, keys, label):
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise ValueError(f"{label} の必須項目が不足しています: {', '.join(missing)}")


def _validate_train_rows(rows, required_keys, label):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{label} に利用可能な列車データがありません")
    for index, train in enumerate(rows, start=1):
        if not isinstance(train, dict):
            raise ValueError(f"{label} の {index} 件目が不正です")
        _require_keys(train, required_keys, f"{label} の {index} 件目")


def _validate_route_cache(name, data):
    if not isinstance(data, dict):
        raise ValueError(f"{name} キャッシュの形式が不正です")
    _require_keys(data, ["schema_version", "service_day"], f"{name} キャッシュ")

    if name == "osaka_monorail_to_kadoma":
        by_station = data.get("by_station")
        if not isinstance(by_station, dict) or not by_station:
            raise ValueError(f"{name} キャッシュに駅別データがありません")
        for station, station_data in by_station.items():
            if not isinstance(station_data, dict):
                raise ValueError(f"{name} キャッシュの {station} が不正です")
            _validate_train_rows(station_data.get("trains"), ["departure", "kadoma_arrival", "type"], f"{station} -> 門真市")
        return

    required = ["departure", "type"]
    if name == "osaka_monorail_stop_times":
        required.append("stops")
    _validate_train_rows(data.get("trains"), required, name)


def _generate_cache(name, service_day, mono_station=None):
    import generate_timetable_cache as generator

    service_date = generator.default_service_date(service_day)
    if name == "keihan_neyagawa_to_kadoma":
        generator.build_keihan_neyagawa_to_kadoma(service_day, service_date)
    elif name == "keihan_kadoma_to_neyagawa":
        generator.build_keihan_kadoma_to_neyagawa(service_day, service_date)
    elif name == "osaka_monorail_stop_times":
        generator.build_monorail_stop_times(service_day, service_date)
    elif name == "osaka_monorail_to_kadoma":
        if not mono_station:
            raise ValueError("osaka_monorail_to_kadoma の生成には mono_station が必要です")
        generator.build_monorail_to_kadoma_station(mono_station, service_day, service_date)
    else:
        raise ValueError(f"未知のキャッシュ名です: {name}")


def _load_cache(name, service_day):
    path = _cache_path(name, service_day)
    if not path.exists() or path.stat().st_size == 0:
        _generate_cache(name, service_day)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    _validate_route_cache(name, data)
    return data


def _load_monorail_to_kadoma_cache(service_day, station):
    name = "osaka_monorail_to_kadoma"
    path = _cache_path(name, service_day)
    if not path.exists() or path.stat().st_size == 0:
        _generate_cache(name, service_day, station)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if station not in data.get("by_station", {}):
        _generate_cache(name, service_day, station)
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    _validate_route_cache(name, data)
    return data


def _time_to_minutes(text):
    return timetable_hhmm_to_minutes(text)


def _after_or_equal(time_text, base_minutes):
    return _departure_minutes_for_base(time_text, base_minutes) >= base_minutes


def _departure_minutes_for_base(time_text, base_minutes):
    minutes = _time_to_minutes(time_text)
    if minutes < 180 and base_minutes > 1200:
        minutes += 24 * 60
    return minutes


def _find_first_after(trains, key, base_minutes):
    for train in trains:
        if _after_or_equal(train[key], base_minutes):
            return train
    return None


def _arrival_minutes(time_text, departure_base):
    minutes = _time_to_minutes(time_text)
    while minutes < departure_base:
        minutes += 24 * 60
    return minutes


def _keihan_arrival(train, target):
    if train["route"] == "direct":
        key = "kadoma_arrival" if target == "門真市" else "neyagawa_arrival"
        return train[key]
    transfer = train["transfer_train"]
    key = "kadoma_arrival" if target == "門真市" else "neyagawa_arrival"
    return transfer[key]


def _keihan_segments(train, origin, target):
    if train["route"] == "direct":
        return [
            {
                "mode": "train",
                "line": "京阪電車",
                "from": origin,
                "to": target,
                "departure": train["departure"],
                "arrival": _keihan_arrival(train, target),
                "type": train["type"],
                "note": "直通",
            }
        ]

    transfer = train["transfer_train"]
    return [
        {
            "mode": "train",
            "line": "京阪電車",
            "from": origin,
            "to": "萱島",
            "departure": train["departure"],
            "arrival": train["kayashima_arrival"],
            "type": train["type"],
            "note": "萱島で乗換",
        },
        {
            "mode": "train",
            "line": "京阪電車",
            "from": "萱島",
            "to": target,
            "departure": transfer["kayashima_departure"],
            "arrival": _keihan_arrival(train, target),
            "type": transfer["type"],
            "note": "",
        },
    ]


def _fallback_find_route(request):
    """C++が未接続の間だけ使う実装。C++側との一致をテストで守る。"""
    direction = request["direction"]
    station = request["mono_station"]
    service_day = request["service_day"]
    now = request["now_minutes"]

    if direction == "to_home":
        return _fallback_to_home(request, station, service_day, now)
    if direction == "from_home":
        return _fallback_from_home(request, station, service_day, now)
    raise ValueError("direction が不正です")


def _fallback_to_home(request, station, service_day, now):
    keihan = _load_cache("keihan_neyagawa_to_kadoma", service_day)["trains"]
    mono = _load_cache("osaka_monorail_stop_times", service_day)["trains"]

    if request["kind"] == "shortest_arrival":
        routes = []
        best = None
        for keihan_train in keihan:
            keihan_departure = _departure_minutes_for_base(keihan_train["departure"], now)
            start_minutes = keihan_departure - request["school_walk_min"]
            candidate = _build_to_home_route(request, station, mono, keihan_train, start_minutes)
            best, routes = _collect_shortest_route(best, routes, candidate)
        return _shortest_response(best, routes)

    school_departure = now
    neyagawa_ready = school_departure + request["school_walk_min"]
    keihan_train = _find_first_after(keihan, "departure", neyagawa_ready)
    if not keihan_train:
        return {"ok": False, "message": "利用できる京阪電車が見つかりませんでした"}
    result = _build_to_home_route(request, station, mono, keihan_train, school_departure)
    return result or {"ok": False, "message": "利用できるモノレールが見つかりませんでした"}


def _build_to_home_route(request, station, mono_trains, keihan_train, school_departure):
    neyagawa_ready = school_departure + request["school_walk_min"]
    keihan_departure = _departure_minutes_for_base(keihan_train["departure"], neyagawa_ready)
    if keihan_departure < neyagawa_ready:
        return None

    kadoma_arrival_text = _keihan_arrival(keihan_train, "門真市")
    kadoma_ready = _arrival_minutes(kadoma_arrival_text, keihan_departure) + request["transfer_min"]
    mono_train = _find_first_after(mono_trains, "departure", kadoma_ready)
    if not mono_train or not mono_train["stops"].get(station):
        return None

    mono_departure = _departure_minutes_for_base(mono_train["departure"], kadoma_ready)
    mono_arrival_text = mono_train["stops"][station]
    end_minutes = _arrival_minutes(mono_arrival_text, mono_departure) + request["home_walk_min"]
    segments = [
        {
            "mode": "walk",
            "line": "徒歩",
            "from": "学校",
            "to": "寝屋川市",
            "departure": minutes_to_hhmm(school_departure),
            "arrival": minutes_to_hhmm(neyagawa_ready),
            "type": "",
            "note": f"{request['school_walk_min']}分",
        },
        *_keihan_segments(keihan_train, "寝屋川市", "門真市"),
        {
            "mode": "train",
            "line": "大阪モノレール",
            "from": "門真市",
            "to": station,
            "departure": mono_train["departure"],
            "arrival": mono_arrival_text,
            "type": mono_train["type"],
            "note": "",
        },
        {
            "mode": "walk",
            "line": "徒歩",
            "from": station,
            "to": "自宅",
            "departure": mono_arrival_text,
            "arrival": minutes_to_hhmm(end_minutes),
            "type": "",
            "note": f"{request['home_walk_min']}分",
        },
    ]
    return _route_response("学校", "自宅", school_departure, end_minutes, segments, "python_fallback")


def _fallback_from_home(request, station, service_day, now):
    mono_data = _load_monorail_to_kadoma_cache(service_day, station)
    by_station = mono_data.get("by_station", {})
    if station not in by_station:
        return {"ok": False, "message": f"{station} -> 門真市 のモノレールキャッシュがありません"}

    mono_trains = by_station[station]["trains"]
    keihan = _load_cache("keihan_kadoma_to_neyagawa", service_day)["trains"]

    if request["kind"] == "shortest_arrival":
        routes = []
        best = None
        for mono_train in mono_trains:
            mono_departure = _departure_minutes_for_base(mono_train["departure"], now)
            start_minutes = mono_departure - request["home_walk_min"]
            candidate = _build_from_home_route(request, station, keihan, mono_train, start_minutes)
            best, routes = _collect_shortest_route(best, routes, candidate)
        return _shortest_response(best, routes)

    home_departure = now
    station_ready = home_departure + request["home_walk_min"]
    mono_train = _find_first_after(mono_trains, "departure", station_ready)
    if not mono_train:
        return {"ok": False, "message": "利用できるモノレールが見つかりませんでした"}
    result = _build_from_home_route(request, station, keihan, mono_train, home_departure)
    return result or {"ok": False, "message": "利用できる京阪電車が見つかりませんでした"}


def _build_from_home_route(request, station, keihan_trains, mono_train, home_departure):
    station_ready = home_departure + request["home_walk_min"]
    mono_departure = _departure_minutes_for_base(mono_train["departure"], station_ready)
    if mono_departure < station_ready:
        return None

    kadoma_ready = _arrival_minutes(mono_train["kadoma_arrival"], mono_departure) + request["transfer_min"]
    keihan_train = _find_first_after(keihan_trains, "departure", kadoma_ready)
    if not keihan_train:
        return None

    neyagawa_arrival_text = _keihan_arrival(keihan_train, "寝屋川市")
    keihan_departure = _departure_minutes_for_base(keihan_train["departure"], kadoma_ready)
    end_minutes = _arrival_minutes(neyagawa_arrival_text, keihan_departure) + request["school_walk_min"]

    segments = [
        {
            "mode": "walk",
            "line": "徒歩",
            "from": "自宅",
            "to": station,
            "departure": minutes_to_hhmm(home_departure),
            "arrival": minutes_to_hhmm(station_ready),
            "type": "",
            "note": f"{request['home_walk_min']}分",
        },
        {
            "mode": "train",
            "line": "大阪モノレール",
            "from": station,
            "to": "門真市",
            "departure": mono_train["departure"],
            "arrival": mono_train["kadoma_arrival"],
            "type": mono_train["type"],
            "note": "",
        },
        *_keihan_segments(keihan_train, "門真市", "寝屋川市"),
        {
            "mode": "walk",
            "line": "徒歩",
            "from": "寝屋川市",
            "to": "学校",
            "departure": neyagawa_arrival_text,
            "arrival": minutes_to_hhmm(end_minutes),
            "type": "",
            "note": f"{request['school_walk_min']}分",
        },
    ]
    return _route_response("自宅", "学校", home_departure, end_minutes, segments, "python_fallback")


def _route_duration(route_result):
    return route_result["total_minutes"]


def _is_better_shortest(candidate, best):
    if best is None:
        return True
    if _route_duration(candidate) != _route_duration(best):
        return _route_duration(candidate) < _route_duration(best)
    if candidate["end_time"] != best["end_time"]:
        return candidate["end_time"] < best["end_time"]
    return candidate["start_time"] < best["start_time"]


def _collect_shortest_route(best, routes, candidate):
    if not candidate or not candidate.get("ok"):
        return best, routes
    if best is None or _route_duration(candidate) < _route_duration(best):
        return candidate, [candidate]
    if _route_duration(candidate) == _route_duration(best):
        routes.append(candidate)
        if _is_better_shortest(candidate, best):
            best = candidate
    return best, routes


def _shortest_response(best, routes):
    if best is None:
        return {"ok": False, "message": "利用できる経路が見つかりませんでした"}
    response = dict(best)
    response["routes"] = routes
    response["route_count"] = len(routes)
    return response


def _route_response(origin, destination, start_minutes, end_minutes, segments, engine):
    return {
        "ok": True,
        "engine": engine,
        "origin": origin,
        "destination": destination,
        "start_time": minutes_to_hhmm(start_minutes),
        "end_time": minutes_to_hhmm(end_minutes),
        "total_minutes": end_minutes - start_minutes,
        "segments": segments,
    }
