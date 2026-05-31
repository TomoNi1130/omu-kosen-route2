from datetime import datetime
import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import route


def test_hhmm_to_minutes_accepts_valid_time():
    assert route.hhmm_to_minutes("08:05") == 485
    assert route.hhmm_to_minutes("23:59") == 1439


@pytest.mark.parametrize("text", ["24:00", "12:60", "bad", "", None])
def test_hhmm_to_minutes_rejects_invalid_time(text):
    with pytest.raises(ValueError):
        route.hhmm_to_minutes(text)


def test_timetable_hhmm_to_minutes_accepts_after_midnight_times():
    assert route.timetable_hhmm_to_minutes("24:07") == 1447


def test_request_rejects_invalid_choice_values():
    kwargs = {
        "kind": "fastest_arrival",
        "direction": "to_home",
        "mono_station": route.AVAILABLE_MONO_STATIONS[0],
        "now_minutes": 480,
        "transfer_min": 3,
        "home_walk_min": 12,
        "school_walk_min": 12,
        "service_day": "weekday",
    }

    with pytest.raises(ValueError):
        route.request(**{**kwargs, "kind": "unknown"})
    with pytest.raises(ValueError):
        route.request(**{**kwargs, "direction": "sideways"})
    with pytest.raises(ValueError):
        route.request(**{**kwargs, "mono_station": "not-a-station"})


def test_normalize_service_day_uses_weekday_from_datetime():
    assert route.normalize_service_day("auto", datetime(2026, 6, 1)) == "weekday"
    assert route.normalize_service_day("auto", datetime(2026, 6, 6)) == "weekend"


def _route_request(direction, kind="fastest_arrival"):
    return route.request(
        kind=kind,
        direction=direction,
        mono_station="千里中央",
        now_minutes=route.hhmm_to_minutes("08:00"),
        transfer_min=3,
        home_walk_min=12,
        school_walk_min=12,
        service_day="weekday",
    )


def _without_engine(payload):
    if isinstance(payload, list):
        return [_without_engine(item) for item in payload]
    if isinstance(payload, dict):
        return {
            key: _without_engine(value)
            for key, value in payload.items()
            if key not in {"engine", "kind"}
        }
    return payload


@pytest.mark.parametrize("direction", ["to_home", "from_home"])
@pytest.mark.parametrize("kind", ["fastest_arrival", "shortest_arrival"])
def test_cpp_core_matches_python_fallback_routes(direction, kind):
    assert route.ensure_cpp_core_built(required=True)
    import route_core

    request = _route_request(direction, kind)
    cpp_result = route_core.find_route(request)
    python_result = route._fallback_find_route(request)

    assert _without_engine(cpp_result) == _without_engine(python_result)


def test_route_cache_validation_rejects_empty_train_rows():
    with pytest.raises(ValueError):
        route._validate_route_cache(
            "keihan_kadoma_to_neyagawa",
            {"schema_version": 1, "service_day": "weekday", "trains": []},
        )


def test_cpp_core_rejects_invalid_cache_time(tmp_path):
    assert route.ensure_cpp_core_built(required=True)
    import route_core

    (tmp_path / "osaka_monorail_to_kadoma_weekday.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_day": "weekday",
                "by_station": {
                    "千里中央": {
                        "trains": [
                            {
                                "departure": "bad",
                                "kadoma_arrival": "08:20",
                                "type": "普通",
                            }
                        ]
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "keihan_kadoma_to_neyagawa_weekday.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_day": "weekday",
                "trains": [
                    {
                        "departure": "08:30",
                        "type": "普通",
                        "route": "direct",
                        "neyagawa_arrival": "08:45",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    request = _route_request("from_home")
    request["cache_dir"] = str(tmp_path)

    with pytest.raises(RuntimeError, match="invalid time format"):
        route_core.find_route(request)
