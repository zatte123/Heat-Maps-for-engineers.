# Heat Maps for Engineers

An interactive map for planning who goes where. It reads the Boards schedule in
`CMS593.accdb`, matches every booking to the engineer's home postcode, and shows:

- **Heat map of where engineers live** (blue) and **where the day's jobs are** (orange).
- **Every booking for the chosen day**, with a line from each engineer's home to their site.
- **Ranked candidates for any job**: click an unassigned job (green) or a booked one
  (to check whether the current booking is the best one). The list shows who should go,
  how they get the materials, when they leave home, when they get to site and when they
  get home. Late arrivals, late finishes and days over contract hours are flagged.
- **Jobs that aren't on the board yet**: type in a postcode and date to see who fits best.

The output is a single HTML file you open in a browser. It doesn't need a server.

## Materials pickups

When a job needs materials from head office, every free engineer is costed three ways.
Each engineer's ranking uses their cheapest way:

| Option | Route | When it wins |
|---|---|---|
| Morning pickup | home → office by **06:00** → stop ≥ **30 min** → site by **07:50** | engineer lives near the office / site is close to it |
| Collect the day before | previous working day's site → office → home, then **straight to site** | site is far from the office, or the morning pickup would be late |
| Already at the office | previous day's job was at an office postcode, so they take the materials home | free, with no extra driving |

The rules are the same as in the timekeeping project. Any postcode starting with `N3`
counts as the office. Lateness is measured against the targets with a 5-minute tolerance.
Fridays use the 14:00 finish, the 15:00 home time and a 9-hour contract (Mon–Thu: 15:00,
16:00, 10 hours). Engineers already booked on another site that day aren't ranked. They
show under "booked elsewhere".

Ranking score = total driving minutes for the job, plus 3 × minutes late on site, plus
1 × minutes late home. Lateness caused by a day-before detour counts against that option.

Drive times are estimates: straight-line distance × 1.3 road factor ÷ 30 km/h. You can
change both in the page under **Travel & timing settings**, along with all the target
times. The page remembers your changes in that browser.

## Setup (Windows, once)

1. Install Python 3.10+ (tick "Add to PATH").
2. Install the **Microsoft Access Database Engine** redistributable. It must match your
   Python's bitness: 64-bit Python needs the 64-bit engine.
3. In this folder: `pip install -r requirements.txt`

## Use

```bat
python -m heatmap inspect          :: 1st time: lists tables/columns and which ones will be used
python -m heatmap build --open     :: reads the board, writes output\engineer_heatmap.html, opens it
```

You can also double-click `run_heatmap.bat`.

`build` loads bookings from 3 days ago to 14 days ahead. Change that with `--start
2026-10-12 --days-back 3 --days-ahead 21` or in the config. Postcodes are looked up
on [postcodes.io](https://postcodes.io), which is free and needs no key. Results are
cached in `postcode_cache.json`, so later runs are fast.

### Pointing it at the right tables

The tool hasn't seen the CMS593 schema yet, so it **guesses** the tables and columns
from their names. For engineers it looks for a name, a home postcode or address, and
an ID. For the board it looks for a date, an engineer, a site postcode or address,
a job number and a materials flag. `inspect` prints the full schema next to its
guesses and saves both to `schema_report.txt`.

If a guess is wrong, copy `heatmap_config.example.json` to `heatmap_config.json` and
fill in the right names, for example:

```json
"board": { "table": "Boards", "date": "WorkDate", "engineer": ["Eng1", "Eng2"], "site_postcode": "SitePC" }
```

- `engineer` can be one column or a list (crews). Values are matched against the
  engineers table's ID **or** name.
- If there's no postcode column, the postcode is taken from the address text.
- `end_date` (optional) spreads multi-day bookings over the working days in between.
- `needs_materials` (optional) is a yes/no column. Without it, every job uses
  `default_needs_materials`. You can tick or untick it per job in the page.
- Set `office_postcode` to the exact head office postcode for accurate office legs.
  Otherwise the centre of `N3` is used.

### Try it without the database

```bash
python -m heatmap demo --open
```

This builds `output/demo_heatmap.html` from made-up engineers and jobs around London.

## Privacy

The generated HTML and `postcode_cache.json` contain engineers' home postcodes. Both
are git-ignored. Keep them on internal drives and don't email them outside the company.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests            # loader, mapping, geocoder, HTML
node --test tests/planner.test.js  # ranking / timing rules
```

| Path | What |
|---|---|
| `heatmap/config.py` | defaults (office prefix, target times, contract hours) and config loading |
| `heatmap/mapping.py` | table/column auto-detection |
| `heatmap/loader.py` | reads engineers and board bookings, geocodes, builds the dataset |
| `heatmap/geocode.py` | postcodes.io lookups with cache and outcode fallback |
| `heatmap/web/planner.js` | the candidate ranking rules (shared by the page and the tests) |
| `heatmap/web/app.js`, `template.html`, `style.css` | the map page |
