# Heat Maps for Engineers

One script, `engineer_heatmap.py`. It reads the board in `CMS593.accdb`, works out where
each fitter lives, and opens an interactive map in your browser:

- **Heat map** of where fitters live (blue) and where the day's jobs are (orange), with the N3 office marked as HQ.
- **The day's board**: each booked fitter's home linked to their site. Fitters who are off
  (Holiday, Sick, BHoliday, Unpaid) are listed as unavailable. You can filter by **ProjectMgr**.
- **Best fitter for a job**: click an unassigned job (green) or a booked one to check it.
  Free fitters are ranked by driving time. Each one shows when they leave home, reach site
  (the board's **StartTime**, or 07:50 if blank) and get home. Late arrivals, late finishes
  and days over contract hours are flagged.
- **Jobs not on the board yet**: type in a postcode and date.

## Run it

```bat
pip install pyodbc
python engineer_heatmap.py
```

Or double-click `engineer_heatmap.py`. You also need the **Microsoft Access Database Engine**
installed, matching your Python's 32- or 64-bit version.

Other options: `--list` shows every table and column, `--demo` builds the map from made-up
data, and `--db PATH` uses a different database. All settings (database path, office prefix,
target times, contract hours, drive speed) are at the top of the script.

## Fitters' home postcodes: `fitter_homes.csv`

Put `fitter_homes.csv` next to the script:

```
FitRef,FitterName,HomePostcode
,Jane Smith,N12 8AB
```

Names are matched loosely against the board's FitterName ("Jane A Smith" matches "Jane Smith"
or "Jane"), or by FitRef if you fill it in. The script also looks for a fitters table in the
database that contains home addresses. Where both have a postcode, the CSV wins. If neither
exists, the script writes a template CSV listing every current fitter for you to fill in.

**This repo is public, so `fitter_homes.csv`, the generated map and `postcode_cache.json`
are git-ignored. Never commit them.**

## Materials from head office

Tick "Needs materials" on a job (the board has no column for this). Each free fitter is then costed three ways:

| Option | Route |
|---|---|
| Morning pickup | home → office by 06:00 → stop ≥ 30 min → site |
| Collect the day before | previous day's site → office → home, then straight to site |
| Already at the office | previous day's job was at an N3 postcode, so no detour |

The rules are the same as the timekeeping project: N3 counts as the office, with a 5-minute
tolerance. The work end and home targets are 15:00/16:00 Mon–Thu and 14:00/15:00 Fri, and
the contract days are 10 h and 9 h. Score = driving minutes + 3 × minutes late on site + 1 ×
minutes late home. Drive time is estimated as straight-line distance × 1.3 ÷ 30 km/h. You
can change that in the page's settings panel.

Postcodes are looked up on postcodes.io, which is free and needs no key. Results are cached
in `postcode_cache.json`.

## Tests

```bash
python -m pytest tests
node --test tests/planner.test.js
```
