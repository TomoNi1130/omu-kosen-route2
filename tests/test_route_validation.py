from datetime import datetime
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
