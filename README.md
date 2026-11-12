# Jibble invoicing export

`jibble_export.py` pulls a month of time entries from the Jibble API and writes a CSV with these columns: client, project, team member, start time, end time, duration (decimal hours), description. See `invoicing.md` for the spec.

## Requirements

- [uv](https://docs.astral.sh/uv/). It installs Python and the script's packages automatically, from the metadata at the top of the script.
- Jibble API credentials: Jibble dashboard → Settings → Organization → API Credentials.

## Credentials

Copy the template and fill in your client ID and secret:

```bash
cp dot.env .env
```

The script reads `.env` from its own folder, so it works from any directory. Variables already set in your shell take precedence over `.env`. `.env` is listed in `.gitignore`, so it won't be committed.

## Running

```bash
uv run jibble_export.py                    # previous calendar month
uv run jibble_export.py --month 2026-10    # a specific month
uv run jibble_export.py --out oct.csv      # custom output file (default: jibble_YYYY-MM.csv)
uv run jibble_export.py --debug            # also print one raw time entry to stderr
```

The script is also executable directly:

```bash
chmod +x jibble_export.py
./jibble_export.py --month 2026-10
```

## How rows are built

Jibble records time in two ways, and the CSV includes both.

**Clocked time** is raw clock-in and clock-out punches. The script turns them into one row per worked session:

- A session runs from a clock-in to the next clock-out.
- A clock-in while already clocked in is a project or task switch. It ends the previous session and starts a new one.
- Project and description come from the clock-in. The description joins the clock-in note, the activity name and the clock-out note.
- Times are each person's local time, as recorded on their clock-in and clock-out.
- A session that crosses local midnight into or out of the month is cut there, and only the part inside the month is counted.

**Hour entries** are durations typed in for a day, with no clock times. They appear as rows with only a date in "start time" and an empty "end time".

Rows that round to 0.00 hours are dropped. Clock-outs with no clock-in, and people still clocked in, are skipped with a warning on stderr.

## Checking against Jibble's report

The total should be close to Jibble's tracked-time report for the month, apart from a few hundredths from rounding each row to two decimals. Jibble's report cuts the month at midnight in each person's timesheet timezone (a Jibble profile setting), while the script cuts at local midnight. A session crossing the month boundary can therefore split differently for someone working away from their timesheet timezone. One more known difference: when clocks go back in autumn, 02:00–03:00 happens twice. For a punch inside that hour, Jibble's report can count an extra hour that the stored timestamps don't support. The script follows the timestamps.
