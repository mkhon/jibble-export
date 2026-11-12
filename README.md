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

Jibble stores raw clock-in and clock-out punches. The script turns them into one row per worked session:

- A session runs from a clock-in to the next clock-out.
- A clock-in while already clocked in is a project or task switch. It ends the previous session and starts a new one.
- Project and description come from the clock-in. The description joins the clock-in note, the activity name and the clock-out note.
- Times are each person's local time, as recorded by Jibble.
- Entries are picked by Jibble's "belongs to date", so a session that runs past midnight stays in the month it started.
- Sessions that round to 0.00 hours are dropped. Clock-outs with no clock-in, and people still clocked in, are skipped with a warning on stderr.
