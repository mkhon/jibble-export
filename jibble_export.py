#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "python-dotenv"]
# ///
"""
Pull a month of time entries from Jibble and write the invoicing CSV
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


def names_by_id(session: requests.Session, url: str, name_field: str = "name") -> dict[str, str]:
    return {r["id"]: r.get(name_field) or "" for r in api_get_all(session, url)}


def fetch_time_entries(session: requests.Session, first: datetime.date, last: datetime.date) -> list[dict]:
    next_day = last + datetime.timedelta(days=1)
    return api_get_all(
        session,
        f"{TIME_TRACKING_API}/TimeEntries",
        params={
            "$filter": f"belongsToDate ge {first.isoformat()} and belongsToDate lt {next_day.isoformat()}",
            "$orderby": "time,id",
        },
    )


def build_sessions(entries: list[dict], activity_names: dict[str, str]) -> tuple[list[dict], int]:
    """Turn raw In/Out punches into worked sessions.

    A clock-in while already clocked in is a project/activity switch, so it
    closes the open session at that moment.
    """
    by_person = defaultdict(list)
    for e in entries:
        if e.get("status") == "Active":
            by_person[e["personId"]].append(e)

    sessions = []
    orphan_outs = 0

    def close(start: dict, end: dict) -> None:
        start_dt = datetime.datetime.fromisoformat(start["localTime"])
        end_dt = datetime.datetime.fromisoformat(end["localTime"])
        texts = [
            start.get("note"),
            activity_names.get(start.get("activityId")),
            end.get("note") if end["type"] == "Out" else None,
        ]
        sessions.append(
            {
                "person_id": start["personId"],
                "project_id": start.get("projectId"),
                "start": start_dt,
                "end": end_dt,
                "duration": (end_dt - start_dt).total_seconds() / 3600,
                "description": "; ".join(dict.fromkeys(t.strip() for t in texts if t and t.strip())),
            }
        )

    for punches in by_person.values():
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
                else:
                    orphan_outs += 1
        if open_in:
            print(
                f"warning: {open_in['personId']} still clocked in since {open_in['localTime']}, skipped",
                file=sys.stderr,
            )
    return sessions, orphan_outs


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

    people = names_by_id(session, f"{WORKSPACE_API}/People", "fullName")
    clients = names_by_id(session, f"{WORKSPACE_API}/Clients")
    activities = names_by_id(session, f"{WORKSPACE_API}/Activities")
    projects = {
        p["id"]: {"name": p.get("name") or "", "client": clients.get(p.get("clientId"), "")}
        for p in api_get_all(session, f"{WORKSPACE_API}/Projects")
    }

    entries = fetch_time_entries(session, first, last)
    if args.debug and entries:
        print(json.dumps(entries[0], indent=2), file=sys.stderr)

    sessions, orphan_outs = build_sessions(entries, activities)
    if orphan_outs:
        print(f"warning: {orphan_outs} clock-out(s) without a matching clock-in, skipped", file=sys.stderr)

    rows = []
    for s in sessions:
        if round(s["duration"], 2) <= 0:
            continue
        proj = projects.get(s["project_id"], {"name": "", "client": ""})
        rows.append(
            {
                "client": proj["client"],
                "project": proj["name"],
                "team member": people.get(s["person_id"], s["person_id"]),
                "start time": f"{s['start']:%Y-%m-%d %H:%M}",
                "end time": f"{s['end']:%Y-%m-%d %H:%M}",
                "duration": f"{s['duration']:.2f}",
                "description": s["description"],
            }
        )
    rows.sort(key=lambda r: (r["client"], r["project"], r["team member"], r["start time"]))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows ({len(entries)} time entries) to {out_path} for {first} .. {last}")


if __name__ == "__main__":
    main()
