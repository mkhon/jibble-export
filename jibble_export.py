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
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv

TOKEN_URL = "https://identity.prod.jibble.io/connect/token"
WORKSPACE_API = "https://workspace.prod.jibble.io/v1"
TIME_TRACKING_API = "https://time-tracking.prod.jibble.io/v1"
PAGE_SIZE = 500
TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 6
MAX_WAIT_SECONDS = 120
DEFAULT_LONG_HOURS = 12
REVIEW_EXIT_CODE = 3

CSV_COLUMNS = [
    "client",
    "project",
    "team member",
    "start time",
    "end time",
    "duration",
    "rate",
    "description",
]
TEXT_COLUMNS = ["client", "project", "team member", "description"]
REVIEW_HEADING = (
    "Needs review: rows below are not included above or in the total. "
    "Each description starts with the reason."
)


class Jibble:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self.authenticate()

    def authenticate(self) -> None:
        resp = self.send(
            lambda: requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=TIMEOUT_SECONDS,
            ),
            TOKEN_URL,
        )
        self.session.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def get(self, url: str, params: dict) -> dict:
        return self.send(lambda: self.session.get(url, params=params, timeout=TIMEOUT_SECONDS), url, relogin=True).json()

    def send(self, request, url: str, relogin: bool = False) -> requests.Response:
        """Retry rate limits, server errors and network failures; re-login once on 401."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last_attempt = attempt == MAX_ATTEMPTS
            try:
                resp = request()
            except (requests.ConnectionError, requests.Timeout) as exc:
                if last_attempt:
                    raise
                self.wait(url, type(exc).__name__, 2**attempt)
                continue
            if resp.status_code == 401 and relogin and not last_attempt:
                relogin = False
                self.authenticate()
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and not last_attempt:
                retry_after = resp.headers.get("Retry-After", "")
                self.wait(url, f"HTTP {resp.status_code}", int(retry_after) if retry_after.isdigit() else 2**attempt)
                continue
            resp.raise_for_status()
            return resp

    @staticmethod
    def wait(url: str, reason: str, seconds: int) -> None:
        seconds = min(seconds, MAX_WAIT_SECONDS)
        print(f"warning: {reason} from {url}, retrying in {seconds} s", file=sys.stderr)
        time.sleep(seconds)

    def get_all(self, url: str, params: dict | None = None) -> list[dict]:
        params = {"$orderby": "id", **(params or {}), "$top": PAGE_SIZE}
        items = []
        skip = 0
        while True:
            batch = self.get(url, {**params, "$skip": skip})["value"]
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items
            skip += PAGE_SIZE


def parse_month(value: str) -> datetime.date:
    m = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    return datetime.date(int(m.group(1)), int(m.group(2)), 1)


def positive_hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError:
        hours = 0.0
    if not 0 < hours < float("inf"):
        raise argparse.ArgumentTypeError(f"expected a positive number of hours, got {value!r}")
    return hours


def clocked_hours_by_day(items: list[dict]) -> dict[tuple[str, str], float]:
    """Clocked hours per (person, local date), splitting sessions that run past midnight."""
    totals = defaultdict(float)
    for it in items:
        if not (it["start"] and it["end"]):
            continue
        start = datetime.datetime.strptime(it["start"], "%Y-%m-%d %H:%M")
        end = datetime.datetime.strptime(it["end"], "%Y-%m-%d %H:%M")
        while start < end:
            midnight = datetime.datetime.combine(start.date() + datetime.timedelta(days=1), datetime.time())
            part_end = min(end, midnight)
            totals[(it["person_id"], start.date().isoformat())] += (part_end - start).total_seconds() / 3600
            start = part_end
    return totals


def month_range(first: datetime.date | None) -> tuple[datetime.date, datetime.date]:
    if first is None:
        first = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def iso_duration_hours(value: str) -> float:
    num = r"(\d+(?:[.,]\d+)?)"
    m = re.fullmatch(rf"P(?:{num}W)?(?:{num}D)?(?:T(?:{num}H)?(?:{num}M)?(?:{num}S)?)?", value)
    if not m or value in ("P", "PT") or value.endswith("T"):
        raise ValueError(f"unexpected duration {value!r}")
    weeks, days, hours, minutes, seconds = (float((x or "0").replace(",", ".")) for x in m.groups())
    return weeks * 168 + days * 24 + hours + minutes / 60 + seconds / 3600


def join_texts(*texts) -> str:
    parts = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            if line.strip():
                parts.append(line.strip())
    return "; ".join(dict.fromkeys(parts))


def spreadsheet_safe(value: str) -> str:
    """Stop spreadsheets from treating a cell as a formula; "- fixed bug" is left alone."""
    risky = value[:1] in ("=", "+", "@", "\t", "\r") or (value[:1] == "-" and value[1:2].strip() != "")
    return "'" + value if risky else value


def local_time(entry: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(entry["localTime"])


def punch_order(entry: dict) -> tuple[datetime.datetime, str]:
    return datetime.datetime.fromisoformat(entry["time"]), entry["id"]


def fmt(moment: datetime.datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M}"


class Names:
    """ID -> name lookups."""

    def __init__(self, kind: str, mapping: dict[str, str]):
        self.kind = kind
        self.mapping = mapping

    def get(self, item_id: str | None, problems: list[str], default: str = "") -> str:
        if not item_id:
            return default
        if item_id not in self.mapping:
            problems.append(f"unknown {self.kind} {item_id}")
            return default
        return self.mapping[item_id]


class Lookups:
    def __init__(self, activities: Names, tasks: Names, locations: Names):
        self.activities = activities
        self.tasks = tasks
        self.locations = locations

    def texts(self, entry: dict, problems: list[str]) -> list:
        return [
            entry.get("note"),
            self.activities.get(entry.get("activityId"), problems),
            self.tasks.get(entry.get("projectTaskId"), problems),
            self.locations.get(entry.get("locationId"), problems),
            entry.get("address"),
        ]


def item(person_id, project_id, start, end, duration, texts, problems) -> dict:
    return {
        "person_id": person_id,
        "project_id": project_id,
        "start": start,
        "end": end,
        "duration": duration,
        "description": join_texts(*texts),
        "problems": problems,
    }


def fetch_time_entries(api: Jibble, first: datetime.date, last: datetime.date) -> list[dict]:
    # Two days of margin catch sessions crossing the month boundary in any timezone.
    since = first - datetime.timedelta(days=2)
    until = last + datetime.timedelta(days=3)
    return api.get_all(
        f"{TIME_TRACKING_API}/TimeEntries",
        params={
            "$filter": f"time ge {since.isoformat()}T00:00:00Z and time lt {until.isoformat()}T00:00:00Z",
            "$orderby": "time,id",
        },
    )


def fetch_closing_punches(api: Jibble, entries: list[dict], last: datetime.date) -> list[dict]:
    """Fetch the next punch for anyone still clocked in at the end of the fetched window.

    Covers clock-outs fixed in Jibble long after the month ended.
    """
    latest = {}
    for e in sorted(entries, key=punch_order):
        if e.get("status") == "Active":
            latest[e["personId"]] = e
    closing = []
    for person_id, e in latest.items():
        if e["type"] != "In" or local_time(e).date() > last:
            continue
        # "ge" plus the (time, id) check keeps a punch with the same timestamp but a later id.
        candidates = api.get(
            f"{TIME_TRACKING_API}/TimeEntries",
            {
                "$filter": f"personId eq {person_id} and status eq 'Active' and time ge {e['time']}",
                "$orderby": "time,id",
                "$top": 10,
            },
        )["value"]
        later = [c for c in candidates if punch_order(c) > punch_order(e)]
        if later:
            closing.append(min(later, key=punch_order))
    return closing


def build_sessions(
    entries: list[dict], first: datetime.date, last: datetime.date, lookups: Lookups, long_hours: float
) -> list[dict]:
    """Turn raw In/Out punches into worked sessions, clipped to the month.

    A clock-in while already clocked in is a project/activity switch, so it
    closes the open session at that moment. A break start also closes it; the
    next clock-in resumes work. Times and month boundaries use
    the local time recorded on the clock-in. Problem punches become items
    with a non-empty "problems" list.
    """
    by_person = defaultdict(list)
    for e in entries:
        if e.get("status") == "Active":
            by_person[e["personId"]].append(e)

    next_day = last + datetime.timedelta(days=1)

    def in_month(entry: dict) -> bool:
        return first <= local_time(entry).date() <= last

    items = []
    for person_id, punches in by_person.items():

        def close(start: dict, end: dict) -> None:
            start_local, end_local = local_time(start), local_time(end)
            # Both edges use the clock-in's offset so adjacent months cut a session at the same instant.
            month_start = datetime.datetime.combine(first, datetime.time(), tzinfo=start_local.tzinfo)
            month_end = datetime.datetime.combine(next_day, datetime.time(), tzinfo=start_local.tzinfo)
            start_dt = max(start_local, month_start).astimezone(start_local.tzinfo)
            end_dt = min(end_local, month_end).astimezone(start_local.tzinfo)
            if end_dt <= start_dt:
                return
            problems = []
            full_hours = (end_local - start_local).total_seconds() / 3600
            if full_hours > long_hours:
                problems.append(f"session over {long_hours:g} h ({full_hours:.2f} h in total)")
            texts = lookups.texts(start, problems)
            if end["type"] == "Out":
                texts += lookups.texts(end, problems)
            items.append(
                item(
                    person_id,
                    start.get("projectId"),
                    fmt(start_dt),
                    fmt(end_dt),
                    (end_dt - start_dt).total_seconds() / 3600,
                    texts,
                    problems,
                )
            )

        punches.sort(key=punch_order)
        open_in = None
        on_break = False
        for p in punches:
            if p["type"] == "In":
                if open_in:
                    close(open_in, p)
                open_in = p
                on_break = False
            elif p["type"] in ("Out", "StartBreak"):
                if open_in:
                    close(open_in, p)
                    open_in = None
                elif p["type"] == "Out" and not on_break and in_month(p):
                    problems = ["clock-out with no clock-in"]
                    texts = lookups.texts(p, problems)
                    items.append(item(person_id, p.get("projectId"), "", fmt(local_time(p)), None, texts, problems))
                on_break = p["type"] == "StartBreak"
            elif in_month(p):
                problems = [f"unknown punch type {p['type']!r}"]
                texts = lookups.texts(p, problems)
                items.append(item(person_id, p.get("projectId"), fmt(local_time(p)), "", None, texts, problems))
        if open_in and local_time(open_in).date() <= last:
            problems = ["still clocked in, no clock-out yet"]
            texts = lookups.texts(open_in, problems)
            start = max(local_time(open_in), datetime.datetime.combine(first, datetime.time(), tzinfo=local_time(open_in).tzinfo))
            items.append(item(person_id, open_in.get("projectId"), fmt(start), "", None, texts, problems))
    return items


def fetch_hour_entries(
    api: Jibble, first: datetime.date, last: datetime.date, lookups: Lookups, long_hours: float
) -> list[dict]:
    """Manually entered durations (no clock times), one per person/day/project."""
    entries = api.get_all(
        f"{TIME_TRACKING_API}/HourEntries",
        params={"$filter": f"date ge {first.isoformat()} and date le {last.isoformat()}"},
    )
    items = []
    for e in entries:
        if e.get("status") != "Active":
            continue
        problems = []
        try:
            duration = iso_duration_hours(e["duration"])
        except ValueError:
            duration = None
            problems.append(f"unreadable duration {e['duration']!r}")
        if duration is not None and duration > long_hours:
            problems.append(f"hour entry over {long_hours:g} h")
        texts = lookups.texts(e, problems)
        items.append(item(e["personId"], e.get("projectId"), e["date"], "", duration, texts, problems))
    return items


def names_by_id(api: Jibble, url: str, name_field: str = "name") -> dict[str, str]:
    return {r["id"]: r.get(name_field) or "" for r in api.get_all(url)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", type=parse_month, help="YYYY-MM, defaults to previous calendar month")
    parser.add_argument("--out", help="output CSV path, defaults to jibble_YYYY-MM.csv")
    parser.add_argument(
        "--long-hours",
        type=positive_hours,
        default=DEFAULT_LONG_HOURS,
        help=f"flag sessions and hour entries longer than this for review (default {DEFAULT_LONG_HOURS})",
    )
    parser.add_argument("--debug", action="store_true", help="print one raw time entry to stderr")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    client_id = os.environ.get("JIBBLE_CLIENT_ID")
    client_secret = os.environ.get("JIBBLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set JIBBLE_CLIENT_ID and JIBBLE_CLIENT_SECRET in .env (see dot.env) or the environment.")

    first, last = month_range(args.month)
    out_path = args.out or f"jibble_{first:%Y-%m}.csv"

    api = Jibble(client_id, client_secret)
    people_rows = api.get_all(f"{WORKSPACE_API}/People")
    people = Names("person", {p["id"]: p.get("fullName") or "" for p in people_rows})
    rates = {p["id"]: p.get("IPersonSetting/BillableRate") for p in people_rows}
    clients = Names("client", names_by_id(api, f"{WORKSPACE_API}/Clients"))
    lookups = Lookups(
        Names("activity", names_by_id(api, f"{WORKSPACE_API}/Activities")),
        Names("project task", names_by_id(api, f"{WORKSPACE_API}/ProjectTasks")),
        Names("location", names_by_id(api, f"{WORKSPACE_API}/Locations")),
    )
    projects = {p["id"]: p for p in api.get_all(f"{WORKSPACE_API}/Projects")}
    project_names = Names("project", {pid: p.get("name") or "" for pid, p in projects.items()})

    entries = fetch_time_entries(api, first, last)
    entries += fetch_closing_punches(api, entries, last)
    if args.debug and entries:
        print(json.dumps(entries[0], indent=2), file=sys.stderr)
    items = [
        it
        for it in build_sessions(entries, first, last, lookups, args.long_hours)
        + fetch_hour_entries(api, first, last, lookups, args.long_hours)
        if it["duration"] is None or round(it["duration"], 2) > 0
    ]

    clocked = clocked_hours_by_day(items)
    for it in items:
        if not it["end"] and it["duration"] is not None and (it["person_id"], it["start"]) in clocked:
            it["problems"].append(f"same day also has {clocked[(it['person_id'], it['start'])]:.2f} h clocked")

    rows, review = [], []
    for it in items:
        problems = it["problems"]
        project_id = it["project_id"]
        row = {
            "client": clients.get(projects.get(project_id, {}).get("clientId"), problems),
            "project": project_names.get(project_id, problems),
            "team member": people.get(it["person_id"], problems, it["person_id"]),
            "start time": it["start"],
            "end time": it["end"],
            "duration": "" if it["duration"] is None else f"{it['duration']:.2f}",
            "rate": "",
            "description": it["description"],
        }
        rate = rates.get(it["person_id"])
        if rate is None:
            if it["person_id"] in rates:
                problems.append("no billable rate")
        else:
            row["rate"] = f"{rate:.2f}"
        if problems:
            # Hours move into the description so summing the duration column never counts review rows.
            hours = f"{row['duration']} h" if row["duration"] else ""
            row["description"] = join_texts("; ".join(dict.fromkeys(problems)), hours, row["description"])
            row["duration"] = ""
            review.append(row)
        else:
            rows.append(row)
    order = lambda r: (r["client"], r["project"], r["team member"], r["start time"] or r["end time"])
    rows.sort(key=order)
    review.sort(key=order)

    def safe(row: dict) -> dict:
        return {k: spreadsheet_safe(v) if k in TEXT_COLUMNS else v for k, v in row.items()}

    # utf-8-sig adds a byte-order mark so Excel reads non-ASCII names correctly.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(safe(r) for r in rows)
        if review:
            f.write("\r\n")
            csv.writer(f).writerow([REVIEW_HEADING])
            writer.writerows(safe(r) for r in review)

    clocked = sum(1 for r in rows if r["end time"])
    total = sum(float(r["duration"]) for r in rows)
    print(
        f"Wrote {len(rows)} rows to {out_path} for {first} .. {last}: "
        f"{clocked} clocked sessions, {len(rows) - clocked} hour entries, {total:.2f} h total"
    )
    if review:
        print(f"{len(review)} rows need review, listed at the bottom of the CSV", file=sys.stderr)
        sys.exit(REVIEW_EXIT_CODE)


if __name__ == "__main__":
    main()
