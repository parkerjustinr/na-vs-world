#!/usr/bin/env python3
"""Pull every series and game an NA team played at a Riot international from
Leaguepedia's Cargo API and write data/tournaments.json and data/matches.json.

Usage:
    python3 scripts/pull_leaguepedia.py            # pull (uses scripts/.cache/)
    python3 scripts/pull_leaguepedia.py --refresh  # pull, ignoring the cache
    python3 scripts/pull_leaguepedia.py --report   # summarize data/matches.json

Login: set LEAGUEPEDIA_USER ("Name@navsworld", from Special:BotPasswords) and
LEAGUEPEDIA_BOT_PASSWORD in the environment. They are read from the
environment only, never printed, and never written to the cache. If they are
unset, the script warns once and runs anonymously, which is rate-limited.

---------------------------------------------------------------------------
CARGO TABLES AND FIELDS USED
---------------------------------------------------------------------------
NOT VERIFIED LIVE: when this script was written, the sandbox could not reach
lol.fandom.com, so these names come from Leaguepedia's documented Cargo
schema and have not been checked against Special:CargoTables. To make up for
that, each run first calls action=cargofields for every table below. If a
REQUIRED field is missing, it stops and prints the table's real field list.
An OPTIONAL field that is missing is left out of the query and reported.

Tournaments (one row per tournament page)
    required: Name, OverviewPage, League, Year
    optional: DateStart, Date (end date), Region, TournamentLevel, IsQualifier
    Used to pick events. Candidates come from LIKE filters on League, Name
    and OverviewPage. Each candidate is then included or excluded by the
    OverviewPage patterns in EVENT_RULES and the keywords in EXCLUDE_PATTERNS.
    * Play-In subpages (".../Play-In") of an included Worlds or MSI event are
      included even if IsQualifier is set or their name says qualifier.
    * Events with Year after the current (ET) year are excluded.
    * Events with no played MatchSchedule rows are excluded.

TournamentRosters (one row per team per tournament page)
    required: Team, OverviewPage, Region
    Region is the region Leaguepedia records for that team AT that
    tournament. This is the source of NA status and opponent_region_raw.

MatchSchedule (one row per series)
    required: MatchId, OverviewPage, Team1, Team2, Winner, Team1Score,
              Team2Score, BestOf, DateTime_UTC
    optional: Tab, Round, IsNullified, FF
    Winner is "1" or "2". Rows with no winner (not played yet) are skipped,
    and so are rows where IsNullified is set.

ScoreboardGames (one row per game played, with stats; no remakes)
    required: GameId, MatchId, OverviewPage, Team1, Team2, WinTeam,
              DateTime_UTC, N_GameInMatch
    optional: Winner
    Joined to series on MatchId.

Series are de-duplicated by MatchId and games by GameId. If an id appears
under several pages (e.g. a parent page and its "/Main Event"), the most
specific page wins.

How NA status is decided (never guessed):
    * Region in NA_REGIONS ("North America", ...)  -> NA
    * Region in AMBIGUOUS_REGIONS ("AME", the 2025 LTA region, and
      "Americas"/"LTA") -> data/team-regions.json decides:
      na_overrides["<year>"]["<team>"] true -> NA, false -> not NA.
      null, or not listed -> unresolved. A team that isn't listed is added
      to the file with null and printed, so its value can be filled in by hand.
    * Empty region, or no TournamentRosters row -> unresolved.
    * Any other non-empty region -> not NA. Every distinct region string is
      printed at the end of a run so this can be checked.

Matches between two NA teams are left out (this is NA vs the world), but they
are listed in the run summary. Remakes are left out because ScoreboardGames
does not record remade games as games.

Cargo returns values HTML-escaped ("&amp;"), so every string is unescaped.
Fields are aliased (e.g. DateTime_UTC=DateTimeUTC) because Cargo otherwise
returns underscores as spaces in the result keys.
---------------------------------------------------------------------------
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_URL = "https://lol.fandom.com/api.php"
WIKI_URL = "https://lol.fandom.com/wiki/"
ENV_USER = "LEAGUEPEDIA_USER"  # bot password user, e.g. "Name@navsworld"
ENV_PASSWORD = "LEAGUEPEDIA_BOT_PASSWORD"
USER_AGENT = "na-vs-world personal fan project (github.com/parkerjustinr/na-vs-world)"
PAGE_LIMIT = 500
SLEEP_SECONDS = 2.0
MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 5.0
RETRYABLE_API_ERRORS = {"ratelimited", "maxlag", "readonly"}
IN_CHUNK = 15  # OverviewPages per IN (...) clause, keeps URLs short

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"

SCHEMA = {
    "Tournaments": {
        "alias": "T",
        "required": ["Name", "OverviewPage", "League", "Year"],
        "optional": ["DateStart", "Date", "Region", "TournamentLevel", "IsQualifier"],
    },
    "TournamentRosters": {
        "alias": "TR",
        "required": ["Team", "OverviewPage", "Region"],
        "optional": [],
    },
    "MatchSchedule": {
        "alias": "MS",
        "required": ["MatchId", "OverviewPage", "Team1", "Team2", "Winner",
                     "Team1Score", "Team2Score", "BestOf", "DateTime_UTC"],
        "optional": ["Tab", "Round", "IsNullified", "FF"],
    },
    "ScoreboardGames": {
        "alias": "SG",
        "required": ["GameId", "MatchId", "OverviewPage", "Team1", "Team2",
                     "WinTeam", "DateTime_UTC", "N_GameInMatch"],
        "optional": ["Winner"],
    },
}

# Event selection. The first matching rule decides event_type; min_year applies.
EVENT_RULES = [
    ("worlds", re.compile(r"^(?:Season [123] World Championship|\d{4} Season World Championship)(?:/|$)"), 2011),
    ("msi", re.compile(r"^\d{4} Mid-Season Invitational(?:/|$)"), 2015),
    ("first_stand", re.compile(r"^\d{4} First Stand(?:/|$)"), 2025),
]
EXCLUDE_PATTERNS = [
    (re.compile(r"qualif", re.I), "qualifier"),
    (re.compile(r"all[- ]?stars?", re.I), "All-Stars"),
    (re.compile(r"rift rivals", re.I), "Rift Rivals"),
    (re.compile(r"esports world cup|\bewc\b", re.I), "Esports World Cup"),
    (re.compile(r"\biem\b|intel extreme", re.I), "IEM"),
    (re.compile(r"show ?match", re.I), "showmatch"),
]
CANDIDATE_LIKES = ["World Championship", "Mid-Season", "First Stand",
                   "All-Star", "Rift Rivals", "Esports World Cup"]

NA_REGIONS = {"north america", "na", "lta north"}
# Regions that don't say which league a team came from: "AME" is what
# Leaguepedia records for 2025 LTA teams, North and South alike. A team with
# one of these is NA only if data/team-regions.json says so (na_overrides).
AMBIGUOUS_REGIONS = {"ame", "americas", "lta"}
TEAM_REGIONS_FILE = "team-regions.json"

KOREAN = re.compile(r"korea|\bkr\b|\blck\b", re.I)
CHINESE = re.compile(r"china|chinese|\bcn\b|\blpl\b", re.I)
GAP_DAYS = 365


class CargoError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# HTTP clients
# --------------------------------------------------------------------------

class LiveClient:
    """Polite Cargo client: one request at a time, 2s apart, cached, backoff.

    Logs in with a bot password from LEAGUEPEDIA_USER / LEAGUEPEDIA_BOT_PASSWORD
    (environment only) before its first network request, and uses that one
    session for every call. Credentials are never printed, logged or cached.
    """

    def __init__(self, refresh=False, env=None):
        import requests  # only needed for live pulls
        self.requests = requests
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.refresh = refresh
        self.last_call = 0.0
        self.live_calls = 0
        self.cached_calls = 0
        self.logged_in = False
        env = os.environ if env is None else env
        self._user = env.get(ENV_USER) or ""
        self._password = env.get(ENV_PASSWORD) or ""
        if not (self._user and self._password):
            missing = [n for n, v in ((ENV_USER, self._user), (ENV_PASSWORD, self._password)) if not v]
            print(f"warning: {' and '.join(missing)} not set; running anonymously "
                  "(anonymous Cargo calls are rate-limited)", file=sys.stderr)
            self._user = self._password = ""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, params, name):
        params = {**params, "format": "json"}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
        path = CACHE_DIR / f"{name}_{key}.json"
        if not self.refresh and path.exists():
            self.cached_calls += 1
            return json.loads(path.read_text(encoding="utf-8"))
        if self._user and not self.logged_in:
            self._login()
        text, data = self._fetch(params)
        path.write_text(text, encoding="utf-8")
        self.live_calls += 1
        return data

    def _login(self):
        _, data = self._fetch({"action": "query", "meta": "tokens", "type": "login", "format": "json"})
        token = data.get("query", {}).get("tokens", {}).get("logintoken")
        if not token:
            raise CargoError("login failed: no login token returned")
        _, data = self._fetch(None, form={"action": "login", "lgname": self._user,
                                          "lgpassword": self._password, "lgtoken": token,
                                          "format": "json"})
        result = data.get("login", {}).get("result")
        if result != "Success":
            # The server's reason text is not printed: it can echo the user name.
            raise CargoError(f"login failed (result: {result}). Check {ENV_USER} and {ENV_PASSWORD}.")
        self.logged_in = True
        print("Logged in to Leaguepedia with a bot password.")

    def _fetch(self, params, form=None):
        """GET params, or POST form (login; its body never appears in errors)."""
        error = ""
        for attempt in range(MAX_ATTEMPTS):
            wait = SLEEP_SECONDS - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            retry_after = 0.0
            try:
                if form is None:
                    resp = self.session.get(API_URL, params=params, timeout=60)
                else:
                    resp = self.session.post(API_URL, data=form, timeout=60)
            except self.requests.RequestException as exc:
                error = f"network error: {type(exc).__name__}" if form else f"network error: {exc}"
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    error = f"HTTP {resp.status_code}"
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 0))
                    except ValueError:
                        pass
                elif resp.status_code != 200:
                    body = "" if form else f": {resp.text[:300]}"
                    raise CargoError(f"HTTP {resp.status_code}{body}")
                else:
                    try:
                        data = resp.json()
                    except ValueError:
                        error = "response was not JSON"
                    else:
                        api_error = data.get("error")
                        if not api_error:
                            return resp.text, data
                        if api_error.get("code") not in RETRYABLE_API_ERRORS:
                            raise CargoError(f"API error: {api_error.get('code') if form else api_error}")
                        error = f"API error {api_error.get('code')}"
            finally:
                self.last_call = time.monotonic()
            delay = max(retry_after, BACKOFF_BASE_SECONDS * 2 ** attempt)
            print(f"  {error}; retrying in {delay:.0f}s ({attempt + 1}/{MAX_ATTEMPTS})", file=sys.stderr)
            time.sleep(delay)
        raise CargoError(f"giving up after {MAX_ATTEMPTS} attempts: {error}")


class FixtureClient:
    """Offline client for parser tests. Serves <dir>/<name>.json for each
    named query (ignoring where/offset) and <dir>/cargofields.json for field
    checks. Each fixture is returned once; later pages and chunks get []."""

    def __init__(self, fixture_dir):
        self.dir = Path(fixture_dir)
        self.served = set()

    def get(self, params, name):
        if params["action"] == "cargofields":
            fields = json.loads((self.dir / "cargofields.json").read_text(encoding="utf-8"))
            return {"cargofields": {f: {"type": "String"} for f in fields[params["table"]]}}
        if name in self.served:
            return {"cargoquery": []}
        self.served.add(name)
        path = self.dir / f"{name}.json"
        if not path.exists():
            return {"cargoquery": []}
        return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Cargo helpers
# --------------------------------------------------------------------------

def verify_fields(client):
    """Return {table: [fields to query]} after checking them against cargofields."""
    usable, problems = {}, []
    for table, spec in SCHEMA.items():
        data = client.get({"action": "cargofields", "table": table}, f"fields_{table}")
        raw = data.get("cargofields", {})
        actual = set(raw.keys()) if isinstance(raw, dict) else {f.get("name") for f in raw}
        missing = [f for f in spec["required"] if f not in actual]
        if missing:
            problems.append(f"{table}: missing required {missing}. Real fields: {sorted(actual)}")
        dropped = [f for f in spec["optional"] if f not in actual]
        if dropped:
            print(f"  note: {table} has no optional field(s) {dropped}; skipping them")
        usable[table] = spec["required"] + [f for f in spec["optional"] if f in actual]
    if problems:
        raise CargoError("Field names differ from what the script expects:\n  " + "\n  ".join(problems))
    return usable


def field_list(table, fields):
    alias = SCHEMA[table]["alias"]
    return ",".join(f"{alias}.{f}={f.replace('_', '')}" for f in fields)


def cargo_rows(client, name, tables, fields, where, order_by, join_on=None):
    rows, offset = [], 0
    while True:
        params = {"action": "cargoquery", "tables": tables, "fields": fields,
                  "where": where, "order_by": order_by,
                  "limit": PAGE_LIMIT, "offset": offset}
        if join_on:
            params["join_on"] = join_on
        data = client.get(params, name)
        page = [{k: html.unescape(v) if isinstance(v, str) else v
                 for k, v in item.get("title", {}).items()}
                for item in data.get("cargoquery", [])]
        rows.extend(page)
        if len(page) < PAGE_LIMIT:
            return rows
        offset += PAGE_LIMIT


def quoted(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def rows_for_pages(client, name, table, fields, pages, order_by):
    alias = SCHEMA[table]["alias"]
    rows = []
    for chunk in chunked(pages, IN_CHUNK):
        where = f"{alias}.OverviewPage IN ({','.join(quoted(p) for p in chunk)})"
        rows += cargo_rows(client, name, f"{table}={alias}", field_list(table, fields), where, order_by)
    return rows


# --------------------------------------------------------------------------
# Selection and parsing
# --------------------------------------------------------------------------

def truthy(value):
    return str(value or "").strip().lower() in {"1", "yes", "true"}


def to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def year_of(row):
    year = to_int(row.get("Year"))
    if year:
        return year
    page = row.get("OverviewPage", "")
    m = re.match(r"^(\d{4})", page) or re.match(r"^Season ([123])", page)
    if not m:
        return None
    return int(m.group(1)) if len(m.group(1)) == 4 else 2010 + int(m.group(1))


def event_type_of(page):
    for event_type, pattern, min_year in EVENT_RULES:
        if pattern.match(page):
            return event_type, min_year
    return None, None


def check_year(row, event_type, min_year, current_year):
    """Return an exclusion reason for the row's year, or None if it is in range."""
    year = year_of(row)
    if year is None:
        return "no year"
    if year < min_year:
        return f"{event_type} before {min_year}"
    if year > current_year:
        return f"Year {year} is after the current year ({current_year})"
    return None


def classify_tournament(row, current_year):
    """Return (event_type, None) to include or (None, reason) to exclude."""
    page = row.get("OverviewPage", "")
    text = " ".join(str(row.get(k, "")) for k in ("Name", "OverviewPage", "League"))
    for pattern, reason in EXCLUDE_PATTERNS:
        if pattern.search(text):
            return None, reason
    if truthy(row.get("IsQualifier")):
        return None, "qualifier (IsQualifier)"
    if str(row.get("TournamentLevel", "")).strip().lower() == "showmatch":
        return None, "showmatch (TournamentLevel)"
    event_type, min_year = event_type_of(page)
    if not event_type:
        return None, "not Worlds/MSI/First Stand"
    reason = check_year(row, event_type, min_year, current_year)
    return (None, reason) if reason else (event_type, None)


def page_root(page):
    return page.split("/", 1)[0]


def select_tournaments(client, fields, current_year):
    """Pick candidate events. Play-In subpages of an included Worlds/MSI event
    are included whatever their IsQualifier flag or name says. Events with no
    played matches are dropped later, in drop_unplayed()."""
    likes = " OR ".join(f'T.{col} LIKE "%{text}%"'
                        for text in CANDIDATE_LIKES
                        for col in ("League", "Name", "OverviewPage"))
    rows = cargo_rows(client, "tournaments", "Tournaments=T",
                      field_list("Tournaments", fields["Tournaments"]),
                      likes, "T.OverviewPage")
    unique, decisions = {}, {}
    for row in rows:
        page = row.get("OverviewPage", "")
        if page and page not in unique:
            unique[page] = row
            decisions[page] = classify_tournament(row, current_year)

    play_in_roots = {page_root(page) for page, (event_type, _) in decisions.items()
                     if event_type in ("worlds", "msi")}
    for page, (event_type, reason) in decisions.items():
        if event_type or not page.endswith("/Play-In") or page_root(page) not in play_in_roots:
            continue
        event_type, min_year = event_type_of(page)
        if event_type in ("worlds", "msi"):
            year_reason = check_year(unique[page], event_type, min_year, current_year)
            decisions[page] = (None, year_reason) if year_reason else (event_type, None)

    included, excluded = [], []
    for page, row in unique.items():
        event_type, reason = decisions[page]
        base = {"overview_page": page, "name": row.get("Name", ""),
                "league": row.get("League", ""), "year": year_of(row)}
        if event_type:
            included.append({**base, "event_type": event_type,
                             "date_start": row.get("DateStart") or None,
                             "date_end": row.get("Date") or None})
        else:
            excluded.append({**base, "reason": reason})
    sort_key = lambda t: (t["year"] or 0, t["overview_page"])
    return sorted(included, key=sort_key), sorted(excluded, key=sort_key)


def drop_unplayed(included, excluded, match_rows):
    """Move included events with no played (winner set) MatchSchedule rows to excluded."""
    played = {m.get("OverviewPage") for m in match_rows
              if str(m.get("Winner") or "").strip() in ("1", "2")}
    kept = [t for t in included if t["overview_page"] in played]
    for t in included:
        if t["overview_page"] not in played:
            excluded.append({k: t[k] for k in ("overview_page", "name", "league", "year")}
                            | {"reason": "no played matches yet"})
    excluded.sort(key=lambda t: (t["year"] or 0, t["overview_page"]))
    return kept, excluded


def pick_by_id(rows, id_field):
    """One row per id. If an id appears under several pages (e.g. a parent page
    and its /Main Event), keep the most specific page so nothing double-counts."""
    best = {}
    for row in rows:
        key = row.get(id_field, "")
        if not key:
            continue
        rank = (row.get("OverviewPage", "").count("/"), row.get("OverviewPage", ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, row)
    return [row for _, row in best.values()]


def parse_utc(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def eastern():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        sys.exit("No time zone data for America/New_York. On Windows run: py -m pip install tzdata")


def date_et(dt, tz):
    return dt.astimezone(tz).date().isoformat() if dt else None


def same_team(a, b):
    return (a or "").strip().casefold() == (b or "").strip().casefold()


class RegionResolver:
    def __init__(self, roster_rows, na_overrides, tournaments, overrides_path):
        self.regions = {}
        for row in roster_rows:
            key = (row.get("OverviewPage", ""), (row.get("Team") or "").strip().casefold())
            self.regions.setdefault(key, (row.get("Team", ""), (row.get("Region") or "").strip()))
        self.overrides = {str(year): {team.strip().casefold(): value for team, value in teams.items()}
                          for year, teams in (na_overrides or {}).items()}
        self.overrides_path = overrides_path
        self.year = {t["overview_page"]: t["year"] for t in tournaments}
        self.unresolved = {}
        self.missing_overrides = set()  # (year, team) for ambiguous teams not in the file
        self.region_strings = Counter(region or "(empty)" for _, region in self.regions.values())

    def region_raw(self, page, team):
        hit = self.regions.get((page, (team or "").strip().casefold()))
        return hit[1] if hit else ""

    def status(self, page, team):
        """Return 'na', 'non_na' or 'unresolved' for a team at a tournament page."""
        hit = self.regions.get((page, (team or "").strip().casefold()))
        if not hit:
            return self._unresolved(team, page, "no TournamentRosters row for this team at this tournament")
        region = hit[1]
        lowered = region.casefold()
        if not lowered:
            return self._unresolved(team, page, "TournamentRosters.Region is empty")
        if lowered in NA_REGIONS:
            return "na"
        if lowered not in AMBIGUOUS_REGIONS:
            return "non_na"
        year = str(self.year.get(page))
        where = f'{self.overrides_path.name} na_overrides["{year}"]'
        overrides = self.overrides.get(year, {})
        key = (team or "").strip().casefold()
        if key not in overrides:
            self.missing_overrides.add((year, hit[0] or team))
        value = overrides.get(key)
        if value is True:
            return "na"
        if value is False:
            return "non_na"
        # Same reason for missing and null, so seeding the file doesn't change matches.json.
        detail = "needs true or false" if value is None else f"is {value!r}, not true or false"
        return self._unresolved(team, page, f"region '{region}': {where} for this team {detail}")

    def _unresolved(self, team, page, reason):
        self.unresolved[(page, team)] = {"team": team, "tournament": page, "reason": reason}
        return "unresolved"


def load_team_regions(path):
    if not path.exists():
        return {"schema_version": 1, "na_overrides": {}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CargoError(f"{path} is not valid JSON: {exc}")
    if not isinstance(doc.get("na_overrides", {}), dict):
        raise CargoError(f"{path}: na_overrides must be an object")
    doc.setdefault("na_overrides", {})
    return doc


def seed_team_regions(path, doc, missing):
    """Add missing (year, team) pairs with null, keep every existing value, write sorted."""
    overrides = doc["na_overrides"]
    for year, team in missing:
        overrides.setdefault(year, {}).setdefault(team, None)
    doc["na_overrides"] = {year: dict(sorted(overrides[year].items(), key=lambda kv: kv[0].casefold()))
                           for year in sorted(overrides)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_matches(tournaments, match_rows, game_rows, resolver, tz):
    by_page = {t["overview_page"]: t for t in tournaments}
    games_by_match = defaultdict(list)
    for g in pick_by_id(game_rows, "GameId"):
        games_by_match[g.get("MatchId", "")].append(g)

    series, games, notes = [], [], defaultdict(list)
    for m in pick_by_id([m for m in match_rows if m.get("OverviewPage") in by_page], "MatchId"):
        match_id = m.get("MatchId", "")
        page = m.get("OverviewPage", "")
        tour = by_page.get(page)
        if not tour:
            continue
        t1, t2 = m.get("Team1", ""), m.get("Team2", "")
        s1, s2 = resolver.status(page, t1), resolver.status(page, t2)
        if "na" not in (s1, s2):
            continue
        if truthy(m.get("IsNullified")):
            notes["nullified (skipped)"].append(match_id)
            continue
        winner = str(m.get("Winner") or "").strip()
        if winner not in ("1", "2"):
            notes["no winner yet (skipped)"].append(match_id)
            continue
        if s1 == s2 == "na":
            notes["NA vs NA (skipped)"].append(f"{match_id}: {t1} vs {t2}")
            continue
        na_side = 1 if s1 == "na" else 2
        na_team, opponent = (t1, t2) if na_side == 1 else (t2, t1)
        if truthy(m.get("FF")):
            notes["forfeit (kept, no games expected)"].append(match_id)

        dt = parse_utc(m.get("DateTimeUTC"))
        if not dt:
            notes["series missing DateTime_UTC"].append(match_id)
        opp_region = resolver.region_raw(page, opponent)
        match_games = []
        for g in games_by_match.get(match_id, []):
            if same_team(g.get("Team1"), na_team):
                g_side = 1
            elif same_team(g.get("Team2"), na_team):
                g_side = 2
            else:
                notes["game team names don't match series"].append(g.get("GameId", ""))
                continue
            g_winner = str(g.get("Winner") or "").strip()
            if g_winner in ("1", "2"):
                won = int(g_winner) == g_side
            elif g.get("WinTeam"):
                won = same_team(g.get("WinTeam"), na_team)
            else:
                notes["game without winner (skipped)"].append(g.get("GameId", ""))
                continue
            g_dt = parse_utc(g.get("DateTimeUTC")) or dt
            match_games.append({
                "game_id": g.get("GameId", ""), "match_id": match_id,
                "game_n": to_int(g.get("NGameInMatch")), "date_et": date_et(g_dt, tz),
                "na_team": na_team, "opponent": opponent,
                "opponent_region_raw": opp_region, "result": "W" if won else "L",
                "_sort": (g_dt.isoformat() if g_dt else "", match_id, to_int(g.get("NGameInMatch")) or 0),
            })

        score1, score2 = to_int(m.get("Team1Score")), to_int(m.get("Team2Score"))
        na_score, opp_score = (score1, score2) if na_side == 1 else (score2, score1)
        if na_score is None or opp_score is None:
            if match_games:
                na_score = sum(g["result"] == "W" for g in match_games)
                opp_score = len(match_games) - na_score
                notes["series score taken from game rows"].append(match_id)
            else:
                notes["series score missing"].append(match_id)
        elif len(match_games) != na_score + opp_score and not truthy(m.get("FF")):
            notes["game rows don't add up to series score"].append(
                f"{match_id}: score {na_score}-{opp_score}, {len(match_games)} game rows")

        stage_parts = [page.split("/", 1)[1] if "/" in page else ""]
        stage_parts += [m.get("Tab") or "", m.get("Round") or ""]
        stage = " / ".join(dict.fromkeys(p.strip() for p in stage_parts if p and p.strip()))
        series.append({
            "match_id": match_id, "tournament": page, "event_type": tour["event_type"],
            "year": tour["year"], "stage": stage, "date_et": date_et(dt, tz),
            "na_team": na_team, "opponent": opponent, "opponent_region_raw": opp_region,
            "best_of": to_int(m.get("BestOf")), "na_score": na_score, "opp_score": opp_score,
            "result": "W" if int(winner) == na_side else "L",
            "leaguepedia_url": WIKI_URL + quote(page.replace(" ", "_"), safe="/:()'"),
            "_sort": (dt.isoformat() if dt else "", match_id),
        })
        games.extend(match_games)

    series.sort(key=lambda s: s["_sort"])
    games.sort(key=lambda g: g["_sort"])
    for row in series + games:
        del row["_sort"]
    unresolved = sorted(resolver.unresolved.values(), key=lambda u: (u["tournament"], u["team"]))
    return series, games, unresolved, notes


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_json_stable(path, payload):
    """Write payload; keep the old generated_utc if nothing else changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if {**old, "generated_utc": None} == {**payload, "generated_utc": None}:
                payload["generated_utc"] = old.get("generated_utc", payload["generated_utc"])
        except (ValueError, TypeError):
            pass
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pull(client, data_dir):
    tz = eastern()
    print("Checking Cargo field names...")
    fields = verify_fields(client)

    print("Selecting tournaments...")
    current_year = datetime.now(tz).year
    included, excluded = select_tournaments(client, fields, current_year)
    print("Fetching series...")
    match_rows = rows_for_pages(client, "matches", "MatchSchedule", fields["MatchSchedule"],
                                [t["overview_page"] for t in included], "MS.DateTime_UTC, MS.MatchId")
    included, excluded = drop_unplayed(included, excluded, match_rows)
    print(f"\nINCLUDED ({len(included)}):")
    for t in included:
        print(f"  + [{t['event_type']:<11}] {t['year']}  {t['overview_page']}")
    print(f"\nEXCLUDED ({len(excluded)}):")
    for t in excluded:
        print(f"  - {t['year']}  {t['overview_page']}  ({t['reason']})")
    write_json_stable(data_dir / "tournaments.json", {
        "schema_version": 1, "generated_utc": now_utc(), "source": "Leaguepedia Cargo API",
        "included": included, "excluded": excluded,
    })

    pages = [t["overview_page"] for t in included]
    print("\nFetching rosters and games...")
    rosters = rows_for_pages(client, "rosters", "TournamentRosters", fields["TournamentRosters"],
                             pages, "TR.OverviewPage, TR.Team")
    game_rows = rows_for_pages(client, "games", "ScoreboardGames", fields["ScoreboardGames"],
                               pages, "SG.DateTime_UTC, SG.GameId")

    overrides_path = data_dir / TEAM_REGIONS_FILE
    team_regions = load_team_regions(overrides_path)
    resolver = RegionResolver(rosters, team_regions["na_overrides"], included, overrides_path)
    for r in rosters:  # resolve every roster team, not only ones in NA matches
        resolver.status(r.get("OverviewPage", ""), r.get("Team", ""))
    series, games, unresolved, notes = build_matches(included, match_rows, game_rows, resolver, tz)
    seed_team_regions(overrides_path, team_regions, resolver.missing_overrides)

    write_json_stable(data_dir / "matches.json", {
        "schema_version": 1, "generated_utc": now_utc(), "source": "Leaguepedia Cargo API",
        "series": series, "games": games, "unresolved": unresolved,
    })

    print(f"\nWrote {len(series)} series, {len(games)} games, {len(unresolved)} unresolved "
          f"to {data_dir / 'matches.json'}")
    print("\nRegion strings seen in TournamentRosters (count):")
    for region, n in sorted(resolver.region_strings.items()):
        print(f"  {region}: {n}")
    for label, items in sorted(notes.items()):
        print(f"\n{label}: {len(items)}")
        for item in items[:20]:
            print(f"  {item}")
        if len(items) > 20:
            print(f"  ... {len(items) - 20} more")
    if resolver.missing_overrides:
        print(f"\nAMBIGUOUS-REGION TEAMS MISSING FROM {overrides_path} "
              f"(added with null; set true for NA, false otherwise):")
        for year, team in sorted(resolver.missing_overrides):
            print(f"  {year}: {team}")
    if unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}):")
        for u in unresolved:
            print(f"  {u['tournament']}: {u['team']} - {u['reason']}")
    return series, games, unresolved


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def report(data_dir):
    path = data_dir / "matches.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    series, games = data["series"], data["games"]
    by_id = {s["match_id"]: s for s in series}

    def game_line(g):
        s = by_id.get(g["match_id"], {})
        return (f"  {g['date_et']}  {g['na_team']} vs {g['opponent']} "
                f"[{g['opponent_region_raw'] or '?'}]  G{g['game_n']} {g['result']}  "
                f"({s.get('tournament', '?')}, {s.get('stage', '')})")

    print(f"Report on {path} (generated {data.get('generated_utc')})\n")
    print("1. NA series and games per event per year")
    counts = defaultdict(lambda: [0, 0])
    for s in series:
        counts[(s["year"], s["event_type"])][0] += 1
    for g in games:
        s = by_id.get(g["match_id"])
        if s:
            counts[(s["year"], s["event_type"])][1] += 1
    print(f"  {'year':<6}{'event':<13}{'series':>7}{'games':>7}")
    for (year, event), (n_s, n_g) in sorted(counts.items()):
        print(f"  {year:<6}{event:<13}{n_s:>7}{n_g:>7}")

    print("\n1b. NA series and games per tournament (! = included tournament with 0 games)")
    per_page = defaultdict(lambda: [0, 0])
    for s in series:
        per_page[s["tournament"]][0] += 1
    for g in games:
        s = by_id.get(g["match_id"])
        if s:
            per_page[s["tournament"]][1] += 1
    tournaments_path = data_dir / "tournaments.json"
    included = []
    if tournaments_path.exists():
        included = json.loads(tournaments_path.read_text(encoding="utf-8")).get("included", [])
    else:
        print(f"  ({tournaments_path} not found; listing only tournaments in matches.json)")
    years = {t["overview_page"]: t["year"] for t in included}
    years.update({s["tournament"]: s["year"] for s in series})
    for page in sorted(years, key=lambda p: (years[p] or 0, p)):
        n_s, n_g = per_page[page]
        flag = "!" if n_g == 0 else " "
        print(f"  {flag} {page:<55}{n_s:>5} series{n_g:>5} games")
    zero = [p for p in years if per_page[p][1] == 0]
    print(f"  {len(zero)} included tournament(s) with 0 NA games")
    for kind, key in (("series", "match_id"), ("games", "game_id")):
        dupes = [k for k, n in Counter(x[key] for x in data[kind]).items() if n > 1]
        if dupes:
            print(f"  WARNING: duplicate {key}s in {kind}: {dupes[:10]}")

    wins = sum(g["result"] == "W" for g in games)
    print(f"\n2. Total NA games: {wins}-{len(games) - wins} (W-L)")

    print("\n3. 15 most recent NA game wins")
    won = [g for g in games if g["result"] == "W"]
    for g in reversed(won[-15:]):
        print(game_line(g))

    hle = "Hanwha Life Esports"
    print(f"\n4. NA games on 2026-07-11 vs {hle}")
    hits = [g for g in games if g["date_et"] == "2026-07-11" and same_team(g["opponent"], hle)]
    for g in hits:
        print(game_line(g))
    if not hits:
        print("  (none)")

    print(f"\n5. NA games vs {hle} at Worlds 2024")
    hits = [g for g in games if same_team(g["opponent"], hle)
            and by_id.get(g["match_id"], {}).get("event_type") == "worlds"
            and by_id.get(g["match_id"], {}).get("year") == 2024]
    for g in hits:
        print(game_line(g))
    if not hits:
        print("  (none)")

    def is_kr_cn(region):
        return bool(KOREAN.search(region or "") or CHINESE.search(region or ""))

    def series_line(s):
        return (f"  {s['date_et']}  {s['na_team']} {s['na_score']}-{s['opp_score']} {s['opponent']} "
                f"[{s['opponent_region_raw']}]  ({s['tournament']}, {s['stage']})")

    regions = {x["opponent_region_raw"] or "" for x in series + games}
    print("\n6. Most recent NA wins vs opponents whose raw region looks Korean or Chinese")
    print(f"  raw region strings matched as KR: {sorted(r for r in regions if KOREAN.search(r)) or '(none)'}")
    print(f"  raw region strings matched as CN: {sorted(r for r in regions if CHINESE.search(r)) or '(none)'}")
    print(f"  raw region strings not matched:   {sorted(r for r in regions if not is_kr_cn(r)) or '(none)'}")
    for label, test in (("KR or CN", is_kr_cn), ("KR", KOREAN.search), ("CN", CHINESE.search)):
        kc_games = [g for g in won if test(g["opponent_region_raw"] or "")]
        kc_series = [s for s in series if s["result"] == "W" and test(s["opponent_region_raw"] or "")]
        print(f"  {label}")
        print("    game:  " + (game_line(kc_games[-1]).strip() if kc_games else "(none)"))
        print("    series:" + (series_line(kc_series[-1])[1:] if kc_series else " (none)"))

    print(f"\n7. Gaps over {GAP_DAYS} days between consecutive NA game wins vs KR/CN (ET dates)")
    kc_wins = [g for g in won if is_kr_cn(g["opponent_region_raw"]) and g["date_et"]]
    day = lambda g: datetime.strptime(g["date_et"], "%Y-%m-%d").date()
    gaps = [(prev, nxt, (day(nxt) - day(prev)).days) for prev, nxt in zip(kc_wins, kc_wins[1:])]
    long_gaps = [gap for gap in gaps if gap[2] > GAP_DAYS]
    for prev, nxt, days in long_gaps:
        print(f"  {days} days")
        print(f"    from {game_line(prev).strip()}")
        print(f"    to   {game_line(nxt).strip()}")
    if not long_gaps:
        print("  (none)")
    if kc_wins:
        today = datetime.now(eastern()).date()
        days = (today - day(kc_wins[-1])).days
        print(f"  open: {days} days from the last win ({kc_wins[-1]['date_et']}) to today ({today.isoformat()})")
    print(f"  ({len(kc_wins)} KR/CN game wins; days = end date minus start date)")

    print("\n8. NA teams per included tournament (check for missing teams)")
    na_teams, unresolved_teams = defaultdict(set), defaultdict(set)
    for s in series:
        na_teams[s["tournament"]].add(s["na_team"])
    for u in data.get("unresolved", []):
        unresolved_teams[u["tournament"]].add(u["team"])
    for page in sorted(years, key=lambda p: (years[p] or 0, p)):
        teams = ", ".join(sorted(na_teams[page], key=str.casefold)) or "(none)"
        print(f"  {page}: {teams}")
        if unresolved_teams[page]:
            print(f"      unresolved here: {', '.join(sorted(unresolved_teams[page], key=str.casefold))}")
    print("  (from series against non-NA opponents; an NA team with only NA-vs-NA matches won't appear)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--refresh", action="store_true", help="ignore scripts/.cache/ and refetch")
    parser.add_argument("--report", action="store_true", help="summarize data/matches.json (and data/tournaments.json) and exit")
    parser.add_argument("--fixtures", metavar="DIR", help="offline: serve hand-written responses from DIR")
    parser.add_argument("--data-dir", metavar="DIR", default=str(DEFAULT_DATA_DIR),
                        help="where JSON is written/read (default: data/)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    if args.report:
        report(data_dir)
        return
    if args.fixtures and data_dir.resolve() == DEFAULT_DATA_DIR.resolve():
        parser.error("--fixtures needs --data-dir somewhere other than data/ (fixture data is not real)")
    client = FixtureClient(args.fixtures) if args.fixtures else LiveClient(refresh=args.refresh)
    try:
        pull(client, data_dir)
    except CargoError as exc:
        sys.exit(f"ERROR: {exc}")
    if isinstance(client, LiveClient):
        print(f"\n{client.live_calls} live requests, {client.cached_calls} served from cache")


if __name__ == "__main__":
    main()
