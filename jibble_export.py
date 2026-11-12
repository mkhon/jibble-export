#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "python-dotenv"]
# ///
"""
Pull a month of time from Jibble and write the invoicing CSV
(client, project, team member, start/end time, duration, description).

Setup:
    Put JIBBLE_CLIENT_ID and JIBBLE_CLIENT_SECRET in a .env file next to
    this script (see dot.env), or export them in the environment.
    (Jibble dashboard -> Settings -> Organization -> API Credentials)

Usage:
    uv run jibble_export.py [--month YYYY-MM] [--out FILE] [--debug]
"""
import argparse
import calendar
import csv
import datetime
import json
import os
import re
import sys
from collections import defaultdict

import requests
from dotenv import load_dotenv

TOKEN_URL = "https://identity.prod.jibble.io/connect/token"
WORKSPACE_API = "https://workspace.prod.jibble.io/v1"
TIME_TRACKING_API = "https://time-tracking.prod.jibble.io/v1"
PAGE_SIZE = 500

CSV_COLUMNS = [
    "client",
    "project",
    "team member",
    "start time",
    "end time",
    "duration",
    "description",
]


def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get_all(session: requests.Session, url: str, params: dict | None = None) -> list[dict]:
    items = []
    params = {**(params or {}), "$top": PAGE_SIZE}
    skip = 0
    while True:
        resp = session.get(url, params={**params, "$skip": skip})
        resp.raise_for_status()
        batch = resp.json()["value"]
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        skip += PAGE_SIZE


def month_range(month: str | None) -> tuple[datetime.date, datetime.date]:
    if month:
        year, mon = map(int, month.split("-"))
        first = datetime.date(year, mon, 1)
    else:
        first = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def iso_duration_hours(value: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", value)
    if not m:
        raise ValueError(f"unexpected duration {value!r}")
    days, hours, minutes, seconds = (float(x or 0) for x in m.groups())
    return days * 24 + hours + minutes / 60 + seconds / 3600


def join_texts(*texts: str | None) -> str:
    parts = []
    for text in texts:
        for line in (text or "").splitlines():
            if line.strip():
                parts.append(line.strip())
    return "; ".join(dict.fromkeys(parts))


def local_time(entry: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(entry["localTime"])


def fetch_time_entries(session: requests.Session, first: datetime.date, last: datetime.date) -> list[dict]:
    # Two days of margin catch sessions crossing the month boundary in any timezone.
    since = first - datetime.timedelta(days=2)
    until = last + datetime.timedelta(days=3)
    return api_get_all(
        session,
        f"{TIME_TRACKING_API}/TimeEntries",
        params={
            "$filter": f"time ge {since.isoformat()}T00:00:00Z and time lt {until.isoformat()}T00:00:00Z",
            "$orderby": "time,id",
        },
    )


def build_sessions(
    entries: list[dict],
    activity_names: dict[str, str],
    first: datetime.date,
    last: datetime.date,
) -> list[dict]:
    """Turn raw In/Out punches into worked sessions, clipped to the month.

    A clock-in while already clocked in is a project/activity switch, so it
    closes the open session at that moment. Times and month boundaries use
    the local time recorded on each punch.
    """
    by_person = defaultdict(list)
    for e in entries:
        if e.get("status") == "Active":
            by_person[e["personId"]].append(e)

    next_day = last + datetime.timedelta(days=1)

    def in_month(entry: dict) -> bool:
        return first <= local_time(entry).date() <= last

    sessions = []
    for person_id, punches in by_person.items():

        def close(start: dict, end: dict) -> None:
            start_local, end_local = local_time(start), local_time(end)
            month_start = datetime.datetime.combine(first, datetime.time(), tzinfo=end_local.tzinfo)
            month_end = datetime.datetime.combine(next_day, datetime.time(), tzinfo=start_local.tzinfo)
            start_dt = max(start_local, month_start).astimezone(start_local.tzinfo)
            end_dt = min(end_local, month_end).astimezone(end_local.tzinfo)
            if end_dt <= start_dt:
                return
            sessions.append(
                {
                    "person_id": person_id,
                    "project_id": start.get("projectId"),
                    "start": start_dt,
                    "end": end_dt,
                    "duration": (end_dt - start_dt).total_seconds() / 3600,
                    "description": join_texts(
                        start.get("note"),
                        activity_names.get(start.get("activityId")),
                        end.get("note") if end["type"] == "Out" else None,
                    ),
                }
            )

        punches.sort(key=lambda e: e["time"])
        open_in = None
        for p in punches:
            if p["type"] == "In":
                if open_in:
                    close(open_in, p)
                open_in = p
            elif p["type"] == "Out":
                if open_in:
                    close(open_in, p)
                    open_in = None
                elif in_month(p):
                    print(f"warning: {person_id} clock-out at {p['localTime']} has no clock-in, skipped", file=sys.stderr)
        if open_in and local_time(open_in).date() <= last:
            print(f"warning: {person_id} still clocked in since {open_in['localTime']}, skipped", file=sys.stderr)
    return sessions


def fetch_hour_entries(
    session: requests.Session,
    first: datetime.date,
    last: datetime.date,
    activity_names: dict[str, str],
) -> list[dict]:
    """Manually entered durations (no clock times), one per person/day/project."""
    entries = api_get_all(
        session,
        f"{TIME_TRACKING_API}/HourEntries",
        params={"$filter": f"date ge {first.isoformat()} and date le {last.isoformat()}"},
    )
    return [
        {
            "person_id": e["personId"],
            "project_id": e.get("projectId"),
            "date": e["date"],
            "duration": iso_duration_hours(e["duration"]),
            "description": join_texts(e.get("note"), activity_names.get(e.get("activityId"))),
        }
        for e in entries
        if e.get("status") == "Active"
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="YYYY-MM, defaults to previous calendar month")
    parser.add_argument("--out", help="output CSV path, defaults to jibble_YYYY-MM.csv")
    parser.add_argument("--debug", action="store_true", help="print one raw time entry to stderr")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    client_id = os.environ.get("JIBBLE_CLIENT_ID")
    client_secret = os.environ.get("JIBBLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set JIBBLE_CLIENT_ID and JIBBLE_CLIENT_SECRET in .env (see dot.env) or the environment.")

    first, last = month_range(args.month)
    out_path = args.out or f"jibble_{first:%Y-%m}.csv"

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {get_access_token(client_id, client_secret)}"

    people = api_get_all(session, f"{WORKSPACE_API}/People")
    person_names = {p["id"]: p.get("fullName") or p["id"] for p in people}
    clients = {c["id"]: c.get("name") or "" for c in api_get_all(session, f"{WORKSPACE_API}/Clients")}
    activities = {a["id"]: a.get("name") or "" for a in api_get_all(session, f"{WORKSPACE_API}/Activities")}
    projects = {
        p["id"]: {"name": p.get("name") or "", "client": clients.get(p.get("clientId"), "")}
        for p in api_get_all(session, f"{WORKSPACE_API}/Projects")
    }

    entries = fetch_time_entries(session, first, last)
    if args.debug and entries:
        print(json.dumps(entries[0], indent=2), file=sys.stderr)
    sessions = build_sessions(entries, activities, first, last)
    hour_entries = fetch_hour_entries(session, first, last, activities)

    rows = []
    for s in sessions:
        rows.append((s, f"{s['start']:%Y-%m-%d %H:%M}", f"{s['end']:%Y-%m-%d %H:%M}"))
    for h in hour_entries:
        rows.append((h, h["date"], ""))

    out_rows = []
    for item, start, end in rows:
        if round(item["duration"], 2) <= 0:
            continue
        proj = projects.get(item["project_id"], {"name": "", "client": ""})
        out_rows.append(
            {
                "client": proj["client"],
                "project": proj["name"],
                "team member": person_names.get(item["person_id"], item["person_id"]),
                "start time": start,
                "end time": end,
                "duration": f"{item['duration']:.2f}",
                "description": item["description"],
            }
        )
    out_rows.sort(key=lambda r: (r["client"], r["project"], r["team member"], r["start time"]))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)

    total = sum(s["duration"] for s in sessions) + sum(h["duration"] for h in hour_entries)
    print(
        f"Wrote {len(out_rows)} rows to {out_path} for {first} .. {last}: "
        f"{len(sessions)} clocked sessions, {len(hour_entries)} hour entries, {total:.2f} h total"
    )


if __name__ == "__main__":
    main()
