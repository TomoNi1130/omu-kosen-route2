#!/usr/bin/env python3
"""Prepare the local route-search project in one command.

This script can create/use .venv, install Python requirements, build the
pybind11 extension, generate all timetable cache files, and run a small smoke
check.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
VENV_PYTHON = ROOT_DIR / ".venv" / "bin" / "python"
REQUIREMENTS = ROOT_DIR / "requirements.txt"


def run(command: list[str], *, cwd: Path = ROOT_DIR) -> None:
    logging.info("running: %s", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def using_project_venv() -> bool:
    return Path(sys.prefix).resolve() == (ROOT_DIR / ".venv").resolve()


def switch_to_project_venv(args: argparse.Namespace) -> None:
    if args.no_venv or using_project_venv():
        return

    if not VENV_PYTHON.exists():
        run([sys.executable, "-m", "venv", str(ROOT_DIR / ".venv")])

    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])


def install_requirements(skip_install: bool) -> None:
    if skip_install:
        return
    run([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])


def selected_service_days(args: argparse.Namespace, generator) -> list[tuple[str, str]]:
    dates = {
        "weekday": args.weekday_date or generator.default_service_date("weekday"),
        "weekend": args.weekend_date or generator.default_service_date("weekend"),
    }
    if args.service_day == "both":
        return [("weekday", dates["weekday"]), ("weekend", dates["weekend"])]
    return [(args.service_day, dates[args.service_day])]


def generate_caches(args: argparse.Namespace, generator) -> list[dict[str, object]]:
    if args.skip_cache:
        return []

    outputs: list[dict[str, object]] = []
    for service_day, service_date in selected_service_days(args, generator):
        logging.info("building caches: service_day=%s service_date=%s", service_day, service_date)
        outputs.extend(generator.build_all_caches(service_day, service_date))
    return outputs


def smoke_check(args: argparse.Namespace, route, service_day: str) -> None:
    if args.skip_check:
        return

    now_minutes = route.hhmm_to_minutes(args.check_time)
    for direction in ("to_home", "from_home"):
        request = route.request(
            kind="fastest_arrival",
            direction=direction,
            mono_station=args.check_station,
            now_minutes=now_minutes,
            transfer_min=3,
            home_walk_min=12,
            school_walk_min=12,
            service_day=service_day,
        )
        result = route.find_route(request)
        if not result.get("ok"):
            message = result.get("message", "unknown error")
            raise RuntimeError(f"smoke check failed for {direction}: {message}")
        logging.info(
            "smoke check %s: %s -> %s (%s min)",
            direction,
            result["start_time"],
            result["end_time"],
            result["total_minutes"],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare dependencies, C++ build, and timetable caches.")
    parser.add_argument("--no-venv", action="store_true", help="Use the current Python instead of .venv.")
    parser.add_argument("--skip-install", action="store_true", help="Do not run pip install -r requirements.txt.")
    parser.add_argument("--skip-build", action="store_true", help="Do not build the C++ route_core extension.")
    parser.add_argument("--force-build", action="store_true", help="Rebuild route_core even if it looks current.")
    parser.add_argument("--skip-cache", action="store_true", help="Do not generate timetable cache JSON files.")
    parser.add_argument("--skip-check", action="store_true", help="Do not run route-search smoke checks.")
    parser.add_argument(
        "--service-day",
        choices=["weekday", "weekend", "both"],
        default="both",
        help="Which timetable cache set to build.",
    )
    parser.add_argument("--weekday-date", help="Date for weekday timetable fetches, YYYYMMDD.")
    parser.add_argument("--weekend-date", help="Date for weekend timetable fetches, YYYYMMDD.")
    parser.add_argument("--check-station", default="千里中央", help="Station used by smoke checks.")
    parser.add_argument("--check-time", default="08:00", help="HH:MM time used by smoke checks.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    switch_to_project_venv(args)
    install_requirements(args.skip_install)

    sys.path.insert(0, str(SRC_DIR))
    import generate_timetable_cache
    import route

    if not args.skip_build:
        route.ensure_cpp_core_built(force=args.force_build, required=True)

    outputs = generate_caches(args, generate_timetable_cache)
    first_service_day = selected_service_days(args, generate_timetable_cache)[0][0]
    smoke_check(args, route, first_service_day)

    if outputs:
        print("Generated cache files:")
        for output in outputs:
            print(f"- {output['path']} ({output['count']} records)")
    print("Project setup complete.")


if __name__ == "__main__":
    main()
