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

`context.previous_drought_days` is shown only when `context.verified` is `true`.

### `data/worlds2026.json`

One entry per LCS team, in display order. For each team update `wins`, `losses` and `status`:

- `"alive"`: fill `next` (`round`, `opponent`, `format`, `date`). If the opponent is not drawn yet, set `opponent` to `null` and put the text to show in `placeholder`.
- `"advanced"`: shows "Into the knockouts."
- `"eliminated"`: shows "Out at W-L."

Swiss format rule: `Bo3` when wins or losses equal 2, otherwise `Bo1`. The page logs a console warning if `next.format` disagrees.

## Test URL params

- `?today=YYYY-MM-DD` uses that date as today (ET) for the counter.
- `?celebrate=1` forces celebration mode.

They combine: `?today=2026-11-01&celebrate=1`.

## Checking the date math

`app.js` exports its pure functions when loaded in node:

```sh
node -e "const a=require('./app.js'); console.log(a.daysSince('2026-07-11','2026-10-17'))"
```
