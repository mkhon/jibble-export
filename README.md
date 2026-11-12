# Jibble invoicing export

`jibble_export.py` pulls a month of time entries from the Jibble API and writes a CSV with these columns: client, project, team member, start time, end time, duration (decimal hours), rate, description.

The rate is the person's billable rate from their Jibble profile. Jibble keeps only the current rate, so re-exporting an old month uses today's rates. A person with hours but no rate goes to the review section. See `invoicing.md` for the spec.

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
uv run jibble_export.py --long-hours 14    # flag sessions over 14 h instead of 12
uv run jibble_export.py --debug            # also print one raw time entry to stderr
```

The script is also executable directly:

```bash
chmod +x jibble_export.py
./jibble_export.py --month 2026-10
```

## Summary file

When the output is a regular file, each run also writes `<output>_summary.csv` (e.g. `jibble_2026-10_summary.csv`) with hours, rate and amount (hours × rate) for each client, project and team member. Each client ends with a "Client total" row, and the file ends with a grand total. A missing client or project shows as `(none)`. Rows in the review section aren't included; when there are any, a last "Needs review" line gives their count and hours.

## How rows are built

Jibble records time in two ways, and the CSV includes both.

**Clocked time** is raw clock-in and clock-out punches. The script turns them into one row per worked session:

- A session runs from a clock-in to the next clock-out.
- A clock-in while already clocked in is a project or task switch. It ends the previous session and starts a new one.
- Starting a break ends the session, so break time isn't counted. The next clock-in starts a new session.
- Project comes from the clock-in. The description joins every non-empty text on the clock-in and clock-out: notes, activity, project task, location and address. Repeated text appears once.
- Times are each person's local time, as recorded on their clock-in. If the clock-out was recorded in a different timezone (travel, or a clock change), the end time is converted to the clock-in's timezone, so end minus start matches the duration (to within a minute, since seconds aren't shown).
- A session that crosses local midnight into or out of the month is cut there, and only the part inside the month is counted. Midnight is taken in the clock-in's timezone, so if the clock-out was recorded in a different timezone, the previous and next month's exports still cut the session at the same moment.

**Hour entries** are durations typed in for a day, with no clock times. They appear as rows with only a date in "start time" and an empty "end time". Their description is built the same way.

Rows that round to 0.00 hours are dropped. If someone is clocked in at the end of the month, the script looks up their next punch, however late it was entered.

## Rows that need review

Rows that look wrong go to a separate section at the bottom of the CSV, after an empty row and a row explaining the section. Each one's description starts with the reason, followed by its hours. The duration column is left empty, so summing the whole column gives only billable hours. Reasons:

- a session or hour entry over 12 hours, usually a forgotten clock-out or a typo (change the limit with `--long-hours 14`)
- an hour entry on a day that also has clocked time, which may be double counting. The reason shows that day's clocked hours, counting sessions that run past midnight on each day they cover, and including sessions that are themselves in review
- a clock-out with no clock-in
- someone still clocked in, with no clock-out yet
- an unknown punch type
- an hour entry whose duration can't be read
- an unknown project, client, person, activity, task or location (the name is left blank)

Fix the data in Jibble and re-run, or bill these rows by hand. The total printed at the end is the sum of the main rows' durations. When there are rows to review, the script exits with code 3, so scheduled runs can detect it. (Code 2 means bad command-line arguments.)

pandas reads the explanation row as a data row, with the text in `client`. To load only billable rows, drop rows with an empty `duration`.

## The CSV file

- It's UTF-8 with a byte-order mark, so Excel shows non-English names correctly. Scripts should read it with the `utf-8-sig` encoding, or the first column name starts with an invisible character.
- To stop spreadsheets running text as a formula, text starting with `=`, `+`, `@`, tab or carriage return gets a leading `'`. So does text starting with `-` followed by anything but whitespace, so `-fix` is escaped but `- fixed bug` is not. Spreadsheets may show the `'`.

## Network errors

Each request, including login, times out after 60 seconds. Rate limits (HTTP 429), server errors and dropped connections are retried up to 6 times, with a warning on stderr. Waits follow Jibble's Retry-After header, up to 2 minutes each. If the login token expires mid-run, the script logs in again. If it still fails, the script stops with the HTTP error.

## Checking against Jibble's report

The total should be close to Jibble's tracked-time report for the month, apart from a few hundredths from rounding each row to two decimals. Jibble's report cuts the month at midnight in each person's timesheet timezone (a Jibble profile setting), while the script cuts at local midnight. A session crossing the month boundary can therefore split differently for someone working away from their timesheet timezone. One more known difference: when clocks go back in autumn, 02:00–03:00 happens twice. For a punch inside that hour, Jibble's report can count an extra hour that the stored timestamps don't support. The script follows the timestamps.
