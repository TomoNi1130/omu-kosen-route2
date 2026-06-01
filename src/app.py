from datetime import datetime
import logging
import os
from pathlib import Path

from flask import Flask, render_template, request

import route


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

ROOT_DIR = Path(__file__).resolve().parents[1]
FORM_FIELDS = (
    "direction",
    "station",
    "service_day",
    "transfer_min",
    "home_walk_min",
    "school_walk_min",
    "now_hhmm",
)

app = Flask(
    __name__,
    template_folder=str(ROOT_DIR / "template"),
    static_folder=str(ROOT_DIR / "template"),
)


def fmt_hhmm(minutes):
    if minutes is None:
        return "--:--"
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


app.jinja_env.filters["hhmm"] = fmt_hhmm


def default_form():
    now = datetime.now()
    return {
        "direction": "to_home",
        "station": "千里中央",
        "service_day": "auto",
        "transfer_min": "3",
        "home_walk_min": "12",
        "school_walk_min": "12",
        "now_hhmm": now.strftime("%H:%M"),
    }


def parse_non_negative_int(form, name, label):
    try:
        value = int(form[name])
    except (TypeError, ValueError):
        raise ValueError(f"{label}は0以上の整数で入力してください")
    if value < 0:
        raise ValueError(f"{label}は0以上の整数で入力してください")
    return value


def parse_hhmm(text):
    try:
        return route.hhmm_to_minutes(text)
    except (AttributeError, ValueError):
        raise ValueError("出発時刻は HH:MM 形式で入力してください")


def minutes_until_hhmm(time_text, base_minutes):
    minutes = route.timetable_hhmm_to_minutes(time_text)
    while minutes < base_minutes:
        minutes += 24 * 60
    return minutes - base_minutes


def attach_next_train_wait(route_result, now_minutes):
    if not route_result or not route_result.get("ok"):
        return route_result

    for segment in route_result.get("segments", []):
        if segment.get("mode") != "train":
            continue

        departure = segment.get("departure")
        if not departure:
            continue

        route_result["next_train"] = {
            "line": segment.get("line", ""),
            "type": segment.get("type", ""),
            "from": segment.get("from", ""),
            "to": segment.get("to", ""),
            "departure": departure,
            "wait_minutes": minutes_until_hhmm(departure, now_minutes),
        }
        break

    return route_result


def available_stations():
    return list(route.AVAILABLE_MONO_STATIONS)


def submitted_form(defaults, source):
    form = defaults.copy()
    for field in FORM_FIELDS:
        form[field] = source.get(field, form[field])
    return form


def parse_search_form(form, stations):
    if form["direction"] not in route.DIRECTIONS:
        raise ValueError("方向の指定が不正です")
    if form["station"] not in stations:
        raise ValueError("モノレール駅の指定が不正です")
    if form["service_day"] not in route.SERVICE_DAYS:
        raise ValueError("ダイヤの指定が不正です")

    return {
        "now_minutes": parse_hhmm(form["now_hhmm"]),
        "transfer_min": parse_non_negative_int(form, "transfer_min", "乗換時間"),
        "home_walk_min": parse_non_negative_int(form, "home_walk_min", "自宅側の徒歩時間"),
        "school_walk_min": parse_non_negative_int(form, "school_walk_min", "学校側の徒歩時間"),
    }


def build_route_request(kind, form, values):
    return route.request(
        kind=kind,
        direction=form["direction"],
        mono_station=form["station"],
        service_day=form["service_day"],
        **values,
    )


def run_searches(form, values):
    requests = {
        "fastest": build_route_request("fastest_arrival", form, values),
        "shortest": build_route_request("shortest_arrival", form, values),
    }
    results = {
        "fastest": route.find_route(requests["fastest"]),
        "shortest": route.find_route(requests["shortest"]),
    }
    return requests, results


def attach_waits(results, now_minutes):
    for route_result in results.values():
        attach_next_train_wait(route_result, now_minutes)

    shortest = results.get("shortest") or {}
    for route_result in shortest.get("routes", []):
        attach_next_train_wait(route_result, now_minutes)


def search_error(results):
    fastest = results["fastest"]
    shortest = results["shortest"]
    if fastest.get("ok", False) or shortest.get("ok", False):
        return None
    return fastest.get("message") or shortest.get("message") or "経路が見つかりませんでした"


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@app.route("/", methods=["GET", "POST"])
def index():
    stations = available_stations()
    form = default_form()
    search_requests = {"fastest": None, "shortest": None}
    result = None
    error = None

    if request.method == "POST":
        form = submitted_form(form, request.form)
        try:
            values = parse_search_form(form, stations)
            search_requests, result = run_searches(form, values)
            attach_waits(result, values["now_minutes"])

            error = search_error(result)
            if error:
                result = None
        except ValueError as exc:
            error = str(exc)
        except Exception as exc:
            logging.exception("route search failed")
            if env_flag("FLASK_DEBUG") or env_flag("ROUTE_DEBUG"):
                error = f"検索中にエラーが発生しました: {exc}"
            else:
                error = "検索中にエラーが発生しました。キャッシュや時刻表データを確認してください。"

    return render_template(
        "index.html",
        stations=stations,
        service_days=[
            ("auto", "自動"),
            ("weekday", "平日"),
            ("weekend", "土日"),
        ],
        form=form,
        request_payload=search_requests,
        result=result,
        error=error,
    )


if __name__ == "__main__":
    route.ensure_cpp_core_built(required=False)
    host = "10.133.2.200"
    port = 10071
    debug = env_flag("FLASK_DEBUG") or env_flag("ROUTE_DEBUG")
    app.run(debug=debug, host=host, port=port)
