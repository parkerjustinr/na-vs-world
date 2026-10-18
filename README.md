# na-vs-world

Source for lcsfan.gg, a static fan site. Sprint 0 is one page:

- A counter of days since an LCS team won a game against an LCK or LPL team at a Riot international (Worlds, MSI, First Stand). Games, not series.
- A "This Worlds" strip showing each LCS team's Swiss record and next match.

Plain HTML, CSS and JS. No framework, no build step, no external requests. The page reads only the JSON files in `data/`. It deploys with GitHub Pages from `main`, repo root (`.nojekyll` turns off Jekyll).

## Run locally

The page loads JSON with `fetch`, so it needs a local server (opening `index.html` as a file will not load data):

```sh
python3 -m http.server
```

Then open http://localhost:8000/.

## Updating the data

All dates are calendar dates in America/New_York (ET), written `YYYY-MM-DD`. Edit the JSON by hand, keep it valid, and bump `updated` each time.

### `data/counter.json`

When an LCS team wins a game against an LCK or LPL team at a Riot international, replace `last_win`:

- `date_et`: ET date the game was played. The counter counts from this date.
- `game_end_utc`: optional ISO time the game ended, e.g. `"2026-10-25T03:12:00Z"`. If set, celebration mode runs for 24 hours after it. If `null`, celebration mode runs for the whole ET day of `date_et`.
- `na_team`, `opponent`, `opponent_league` (`LCK` or `LPL`), `event`, `stage`.
- `games_won`: game numbers the LCS team won in the series, e.g. `[2, 3]`.
- `series_score`: from the LCS team's side, e.g. `"2-3"`.
- `series_result`: `"win"` or `"loss"`.
- `sources`: where the result was checked.

`context` (`longest_drought_days`, `longest_drought_from`, `longest_drought_to`, `label`, `source`) is shown only when `context.verified` is `true`, and never in celebration mode. Once the counter passes `longest_drought_days`, the line reads "Past the old record of N days (label)."

### `data/worlds2026.json`

One entry per LCS team, in display order. For each team update `wins`, `losses` and `status`:

- `"alive"`: fill `next` (`round`, `opponent`, `format`, `date`). If the opponent is not drawn yet, set `opponent` to `null` and put the text to show in `placeholder`.
- `"advanced"`: shows "Into the knockouts."
- `"eliminated"`: set `out_stage` (e.g. `"Play-In"`) and optionally `line`. Shows "Out in the {out_stage}." and the line; the card is muted.

Swiss format rule: `Bo3` when wins or losses equal 2, otherwise `Bo1`. The page logs a console warning if `next.format` disagrees.

## Pulling data

`scripts/pull_leaguepedia.py` pulls every series and game an NA team played at a Riot international (Worlds from 2011, MSI from 2015, First Stand from 2025) from Leaguepedia's Cargo API. It writes `data/tournaments.json` (which events were included or excluded, and why) and `data/matches.json`. It needs `requests`, plus `tzdata` on Windows, which has no built-in time zone data.

Windows (cmd):

```bat
py -m pip install requests tzdata
set LEAGUEPEDIA_USER=Name@navsworld
set LEAGUEPEDIA_BOT_PASSWORD=your-bot-password
py scripts\pull_leaguepedia.py
py scripts\pull_leaguepedia.py --report
```

macOS/Linux: `python3 -m pip install requests`, then `export` the same two variables and run `python3 scripts/pull_leaguepedia.py`.

Login is optional but recommended, because anonymous Cargo calls are rate-limited. Create a bot password at https://lol.fandom.com/wiki/Special:BotPasswords. `LEAGUEPEDIA_USER` is the `Name@botname` it shows. Read-only access is enough. The script reads the two variables from the environment only and never prints them. `set` lasts only for that cmd window. Don't put the password in a file in this repo. If either variable is unset, the script warns and runs anonymously.

- Raw API responses are cached in `scripts/.cache/` (gitignored). `--refresh` ignores the cache.
- Requests are paced 2s apart and back off on errors or rate limits.
- Before querying, the script checks its field names against Leaguepedia (`action=cargofields`). If a field it needs is missing, it stops and prints the real field list.
- Teams whose NA status can't be determined go in `unresolved`, never guessed.
- 2025 LTA teams have Leaguepedia region `AME` (North and South alike), so `data/team-regions.json` decides them: under `na_overrides` → `"2025"`, set each team to `true` (NA, LTA North) or `false`. Each pull adds any such team it finds with `null` and prints it. Teams that are `null` or not listed stay in `unresolved` until you fill them in. Re-run the pull after editing.
- `--report` reads only `data/matches.json` and `data/tournaments.json`. It also lists, per tournament, series and game counts (flagging 0 games) and NA team names. It shows the most recent NA wins vs KR and vs CN separately, and every gap over 365 days between NA game wins vs KR/CN.
- Offline parser tests on synthetic fixtures: `python3 -m unittest discover -s scripts`

## Test URL params

- `?today=YYYY-MM-DD` uses that date as today (ET) for the counter.
- `?celebrate=1` forces celebration mode.

They combine: `?today=2026-11-01&celebrate=1`.

## Checking the date math

`app.js` exports its pure functions when loaded in node:

```sh
node -e "const a=require('./app.js'); console.log(a.daysSince('2026-07-11','2026-10-17'))"
```
