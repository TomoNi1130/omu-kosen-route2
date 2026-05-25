import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / "data" / "cache"
BUILD_DIR = ROOT_DIR / "build"
CPP_SOURCE = ROOT_DIR / "src" / "route_core.cpp"
CMAKE_FILE = ROOT_DIR / "CMakeLists.txt"
logger = logging.getLogger(__name__)

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

SERVICE_DAYS = {"auto", "weekday", "weekend"}


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


def hhmm_to_minutes(text):
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def service_day_for_datetime(dt):
    return "weekend" if dt.weekday() >= 5 else "weekday"


def normalize_service_day(service_day, now=None):
    if service_day not in SERVICE_DAYS:
        raise ValueError("service_day は auto / weekday / weekend のいずれかです")
    if service_day != "auto":
        return service_day
    return service_day_for_datetime(now or datetime.now())


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
    pybind11なら route_core.find_fastest_arrival(request_dict)、
    実行ファイルなら標準入力にこのJSONを流す想定です。
    """
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
      1. pybind11 モジュール route_core.find_fastest_arrival(request)
      2. 環境変数 ROUTE_CORE_COMMAND の外部コマンド
      3. Pythonの簡易フォールバック
    """
    if ensure_cpp_core_built():
        try:
            import route_core

            return route_core.find_route(request) #cppへリクエスト
        except ImportError as exc:
            logger.warning("route_core import failed after build: %s", exc)

    command = os.environ.get("ROUTE_CORE_COMMAND")
    if command:
        return _call_route_core_command(command, request)

    return _fallback_find_route(request)


def _call_route_core_command(command, request):
    completed = subprocess.run(
        command.split(),
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _cache_path(name, service_day):
    return CACHE_DIR / f"{name}_{service_day}.json"


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
        return json.load(f)


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
    return data


def _time_to_minutes(text):
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def _after_or_equal(time_text, base_minutes):
    minutes = _time_to_minutes(time_text)
    if minutes < 180 and base_minutes > 1200:
        minutes += 24 * 60
    return minutes >= base_minutes


def _find_first_after(trains, key, base_minutes):
    for train in trains:
        if _after_or_equal(train[key], base_minutes):
            return train
    return None


def _arrival_minutes(time_text, departure_base):
    minutes = _time_to_minutes(time_text)
    if minutes < departure_base % (24 * 60):
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
    """C++が未接続の間だけ使う簡易実装。
    """
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
    school_departure = now
    neyagawa_ready = school_departure + request["school_walk_min"]

    keihan = _load_cache("keihan_neyagawa_to_kadoma", service_day)["trains"]
    keihan_train = _find_first_after(keihan, "departure", neyagawa_ready)
    if not keihan_train:
        return {"ok": False, "message": "利用できる京阪電車が見つかりませんでした"}

    kadoma_arrival_text = _keihan_arrival(keihan_train, "門真市")
    kadoma_ready = _arrival_minutes(kadoma_arrival_text, neyagawa_ready) + request["transfer_min"]

    mono = _load_cache("osaka_monorail_stop_times", service_day)["trains"]
    mono_train = _find_first_after(mono, "departure", kadoma_ready)
    if not mono_train or not mono_train["stops"].get(station):
        return {"ok": False, "message": "利用できるモノレールが見つかりませんでした"}

    mono_arrival_text = mono_train["stops"][station]
    end_minutes = _arrival_minutes(mono_arrival_text, kadoma_ready) + request["home_walk_min"]

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
    home_departure = now
    station_ready = home_departure + request["home_walk_min"]

    mono_data = _load_monorail_to_kadoma_cache(service_day, station)
    by_station = mono_data.get("by_station", {})
    if station not in by_station:
        return {"ok": False, "message": f"{station} -> 門真市 のモノレールキャッシュがありません"}

    mono_train = _find_first_after(by_station[station]["trains"], "departure", station_ready)
    if not mono_train:
        return {"ok": False, "message": "利用できるモノレールが見つかりませんでした"}

    kadoma_ready = _arrival_minutes(mono_train["kadoma_arrival"], station_ready) + request["transfer_min"]

    keihan = _load_cache("keihan_kadoma_to_neyagawa", service_day)["trains"]
    keihan_train = _find_first_after(keihan, "departure", kadoma_ready)
    if not keihan_train:
        return {"ok": False, "message": "利用できる京阪電車が見つかりませんでした"}

    neyagawa_arrival_text = _keihan_arrival(keihan_train, "寝屋川市")
    end_minutes = _arrival_minutes(neyagawa_arrival_text, kadoma_ready) + request["school_walk_min"]

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
