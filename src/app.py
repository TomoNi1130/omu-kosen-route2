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


@app.route("/", methods=["GET", "POST"])
def index():
    stations = [station for station in route.MONO_STATIONS if station != "門真市"]
    form = default_form()
    search_request_fastest = None
    search_request_shortest = None
    result = None
    error = None

    if request.method == "POST":
        form.update(
            {
                "direction": request.form.get("direction", form["direction"]),
                "station": request.form.get("station", form["station"]),
                "service_day": request.form.get("service_day", form["service_day"]),
                "transfer_min": request.form.get("transfer_min", form["transfer_min"]),
                "home_walk_min": request.form.get("home_walk_min", form["home_walk_min"]),
                "school_walk_min": request.form.get("school_walk_min", form["school_walk_min"]),
                "now_hhmm": request.form.get("now_hhmm", form["now_hhmm"]),
            }
        )

        try:
            now_minutes = parse_hhmm(form["now_hhmm"])
            transfer_min = parse_non_negative_int(form, "transfer_min", "乗換時間")
            home_walk_min = parse_non_negative_int(form, "home_walk_min", "自宅側の徒歩時間")
            school_walk_min = parse_non_negative_int(form, "school_walk_min", "学校側の徒歩時間")

            # 最速到着と最短到達の両方を実行して表示する
            search_request_fastest = route.request(
                kind="fastest_arrival",
                direction=form["direction"],
                mono_station=form["station"],
                now_minutes=now_minutes,
                transfer_min=transfer_min,
                home_walk_min=home_walk_min,
                school_walk_min=school_walk_min,
                service_day=form["service_day"],
            )
            search_request_shortest = route.request(
                kind="shortest_arrival",
                direction=form["direction"],
                mono_station=form["station"],
                now_minutes=now_minutes,
                transfer_min=transfer_min,
                home_walk_min=home_walk_min,
                school_walk_min=school_walk_min,
                service_day=form["service_day"],
            )

            res_fastest = route.find_route(search_request_fastest)
            res_shortest = route.find_route(search_request_shortest)

            # エラーハンドリング: 両方失敗したらエラー表示
            if not res_fastest.get("ok", False) and not res_shortest.get("ok", False):
                # 優先して fast のメッセージを表示、なければ short のメッセージ
                error = res_fastest.get("message") or res_shortest.get("message") or "経路が見つかりませんでした"
            else:
                result = {"fastest": res_fastest, "shortest": res_shortest}
        except Exception as exc:
            logging.exception("route search failed")
            error = f"検索中にエラーが発生しました: {exc}"

    return render_template(
        "index.html",
        stations=stations,
        service_days=[
            ("auto", "自動"),
            ("weekday", "平日"),
            ("weekend", "土日"),
        ],
        form=form,
        request_payload={"fastest": search_request_fastest, "shortest": search_request_shortest},
        result=result,
        error=error,
    )


if __name__ == "__main__":
    route.ensure_cpp_core_built(required=False)
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5001"))
    app.run(debug=True, host=host, port=port)
