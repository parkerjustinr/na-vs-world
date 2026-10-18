"""Offline tests for pull_leaguepedia.py against scripts/fixtures/ (synthetic data).

Run: python3 -m unittest discover -s scripts
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pull_leaguepedia as pl  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class PullFromFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.overrides_path = cls.out / pl.TEAM_REGIONS_FILE
        cls.overrides_path.write_text(json.dumps({"schema_version": 1, "note": "kept", "na_overrides": {
            "2025": {"Team Liquid": True, "Vivo Keyd Stars": False}}}))
        cls.stdout = io.StringIO()
        with contextlib.redirect_stdout(cls.stdout):
            pl.pull(pl.FixtureClient(FIXTURES), cls.out)
        cls.matches = json.loads((cls.out / "matches.json").read_text())
        cls.tournaments = json.loads((cls.out / "tournaments.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_tournament_selection(self):
        included = {t["overview_page"] for t in self.tournaments["included"]}
        excluded = {t["overview_page"]: t["reason"] for t in self.tournaments["excluded"]}
        self.assertEqual(pl.classify_tournament(
            {"OverviewPage": "Season 1 World Championship", "Year": "2011"}, 2026), ("worlds", None))
        self.assertIn("2024 Season World Championship/Play-In", included)
        self.assertIn("2025 First Stand", included)
        self.assertEqual(excluded["2023 Season World Championship/Qualifying Series"], "qualifier")
        self.assertEqual(excluded["Esports World Cup 2024"], "Esports World Cup")
        self.assertEqual(excluded["2015 All-Star Event"], "All-Stars")
        self.assertEqual(excluded["Rift Rivals/2019 Season/NA-EU"], "Rift Rivals")

    def test_play_in_included_despite_is_qualifier(self):
        included = {t["overview_page"] for t in self.tournaments["included"]}
        excluded = {t["overview_page"]: t["reason"] for t in self.tournaments["excluded"]}
        self.assertIn("2024 Season World Championship/Play-In", included)
        self.assertIn("2026 Season World Championship/Play-In", included)  # name says "Qualifier"
        # No included 2023 Worlds page, so this Play-In keeps its IsQualifier exclusion.
        self.assertEqual(excluded["2023 Season World Championship/Play-In"], "qualifier (IsQualifier)")

    def test_future_and_unplayed_excluded(self):
        excluded = {t["overview_page"]: t["reason"] for t in self.tournaments["excluded"]}
        self.assertIn("after the current year", excluded["2027 Mid-Season Invitational"])
        self.assertEqual(excluded["Season 1 World Championship"], "no played matches yet")
        self.assertEqual(excluded["2026 Season World Championship"], "no played matches yet")
        self.assertNotIn("MSI2027_1", {s["match_id"] for s in self.matches["series"]})

    def test_no_double_counting(self):
        for kind, key in (("series", "match_id"), ("games", "game_id")):
            ids = [x[key] for x in self.matches[kind]]
            self.assertEqual(len(ids), len(set(ids)), kind)
        series = next(s for s in self.matches["series"] if s["match_id"] == "Worlds2024_Swiss_R1_1")
        self.assertEqual(series["tournament"], "2024 Season World Championship/Main Event")

    def test_lyon_vs_hle_games(self):
        games = [g for g in self.matches["games"]
                 if g["date_et"] == "2026-07-11" and g["opponent"] == "Hanwha Life Esports"]
        self.assertEqual([(g["game_n"], g["result"]) for g in games],
                         [(1, "L"), (2, "W"), (3, "W"), (4, "L"), (5, "L")])
        series = next(s for s in self.matches["series"] if s["match_id"] == "MSI2026_Bracket_LBF")
        self.assertEqual((series["na_team"], series["na_score"], series["opp_score"], series["result"]),
                         ("LYON", 2, 3, "L"))
        self.assertEqual(series["opponent_region_raw"], "Korea")

    def test_date_converted_to_eastern(self):
        series = next(s for s in self.matches["series"] if s["match_id"] == "Worlds2024_Swiss_R1_1")
        self.assertEqual(series["date_et"], "2024-10-04")  # 02:30 UTC on Oct 5

    def test_ame_region_uses_overrides(self):
        fs = [s for s in self.matches["series"] if s["tournament"] == "2025 First Stand"]
        self.assertEqual({s["na_team"] for s in fs}, {"Team Liquid"})
        vks = next(s for s in fs if s["opponent"] == "Vivo Keyd Stars")
        self.assertEqual(vks["opponent_region_raw"], "AME")

    def test_missing_ame_team_seeded_with_null_and_printed(self):
        doc = json.loads(self.overrides_path.read_text())
        self.assertEqual(doc["note"], "kept")
        self.assertEqual(doc["na_overrides"]["2025"],
                         {"Team Liquid": True, "Unknown Americas Team": None, "Vivo Keyd Stars": False})
        self.assertIn("  2025: Unknown Americas Team", self.stdout.getvalue())
        reason = next(u["reason"] for u in self.matches["unresolved"] if u["team"] == "Unknown Americas Team")
        self.assertIn('team-regions.json na_overrides["2025"] for this team needs true or false', reason)

    def test_unresolved_never_guessed(self):
        teams = {u["team"] for u in self.matches["unresolved"]}
        self.assertEqual(teams, {"Mystery Squad", "Unknown Americas Team"})
        self.assertFalse(any(s["na_team"] in teams for s in self.matches["series"]))

    def test_skips(self):
        ids = {s["match_id"] for s in self.matches["series"]}
        self.assertNotIn("Worlds2024_Swiss_R2_1", ids)  # NA vs NA
        self.assertNotIn("Worlds2024_Swiss_R2_2", ids)  # no NA team
        self.assertNotIn("Worlds2024_Swiss_R3_9", ids)  # nullified
        self.assertNotIn("MSI2026_Bracket_GF", ids)     # not played

    def test_sorted_and_stable(self):
        dates = [s["date_et"] for s in self.matches["series"]]
        self.assertEqual(dates, sorted(dates))
        before = (self.out / "matches.json").read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            pl.pull(pl.FixtureClient(FIXTURES), self.out)
        self.assertEqual((self.out / "matches.json").read_text(), before)

    def test_report_gaps_and_teams(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pl.report(self.out)
        out = buf.getvalue()
        # KR/CN wins: 2024-10-05, 2025-03-11, 2026-07-11 (x2), 2026-10-16 -> one gap over 365 days.
        section7 = out.split("\n7.")[1].split("\n8.")[0]
        self.assertIn("  487 days\n", section7)
        self.assertIn("from 2025-03-11  Team Liquid vs Hanwha Life Esports", section7)
        self.assertIn("to   2026-07-11  LYON vs Hanwha Life Esports [Korea]  G2 W", section7)
        self.assertNotIn("157 days", section7)
        self.assertIn("(5 KR/CN game wins", section7)
        section6 = out.split("\n6.")[1].split("\n7.")[0]
        self.assertIn("matched as KR: ['Korea']", section6)
        self.assertIn("matched as CN: ['China']", section6)
        cn = section6.split("  CN\n")[1]
        self.assertIn("2026-10-16  Team Liquid vs Top Esports [China]  G2 W", cn)
        kr = section6.split("  KR\n")[1].split("  CN\n")[0]
        self.assertIn("2026-07-11  LYON vs Hanwha Life Esports [Korea]  G3 W", kr)
        section8 = out.split("\n8.")[1]
        self.assertIn("2024 Season World Championship/Main Event: FlyQuest, Team Liquid\n"
                      "      unresolved here: Mystery Squad", section8)
        self.assertIn("2025 First Stand: Team Liquid\n      unresolved here: Unknown Americas Team", section8)

    def test_report_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pl.report(self.out)
        out = buf.getvalue()
        self.assertIn("Total NA games: 9-10", out)
        self.assertIn("! 2024 Season World Championship ", out)  # parent page: duplicates only
        self.assertIn("1 included tournament(s) with 0 NA games", out)


class TeamRegionsFile(unittest.TestCase):
    def run_pull(self, out):
        with contextlib.redirect_stdout(io.StringIO()):
            pl.pull(pl.FixtureClient(FIXTURES), out)
        return json.loads((out / "matches.json").read_text())

    def test_no_file_seeds_every_ame_team_null_and_resolves_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            matches = self.run_pull(out)
            doc = json.loads((out / pl.TEAM_REGIONS_FILE).read_text())
            self.assertEqual(doc["na_overrides"], {"2025": {
                "Team Liquid": None, "Unknown Americas Team": None, "Vivo Keyd Stars": None}})
            self.assertFalse(any(s["tournament"] == "2025 First Stand" for s in matches["series"]))
            # Second run: listed but null is still unresolved, and nothing changes.
            before = (out / "matches.json").read_text()
            self.run_pull(out)
            self.assertEqual((out / "matches.json").read_text(), before)


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code, self.headers, self.text = status, {}, json.dumps(body)

    def json(self):
        return json.loads(self.text)


class Login(unittest.TestCase):
    USER, PASSWORD = "Someone@navsworld", "s3cret-bot-password"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (pl.CACHE_DIR, pl.SLEEP_SECONDS)
        pl.CACHE_DIR, pl.SLEEP_SECONDS = Path(self.tmp.name), 0
        self.calls = []

    def tearDown(self):
        pl.CACHE_DIR, pl.SLEEP_SECONDS = self.saved
        self.tmp.cleanup()

    def client(self, env, login_result="Success"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            c = pl.LiveClient(env=env)
        c.session.get = lambda url, params, timeout: self._get(params)
        c.session.post = lambda url, data, timeout: self._post(data, login_result)
        return c, out, err

    def _get(self, params):
        self.calls.append(("GET", params))
        if params.get("meta") == "tokens":
            return FakeResponse({"query": {"tokens": {"logintoken": "tok+\\"}}})
        return FakeResponse({"cargoquery": [{"title": {"A": "1"}}]})

    def _post(self, data, result):
        self.calls.append(("POST", data))
        return FakeResponse({"login": {"result": result, "reason": f"bad login for {data['lgname']}"}})

    def run_query(self, c):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rows = pl.cargo_rows(c, "t", "T", "T.A=A", "1=1", "T.A")
            pl.cargo_rows(c, "u", "T", "T.A=A", "2=2", "T.A")
        return rows, out.getvalue() + err.getvalue()

    def test_logs_in_once_then_queries_on_same_session(self):
        c, out, err = self.client({pl.ENV_USER: self.USER, pl.ENV_PASSWORD: self.PASSWORD})
        rows, logs = self.run_query(c)
        self.assertEqual(rows, [{"A": "1"}])
        kinds = [(m, p.get("action"), p.get("meta")) for m, p in self.calls]
        self.assertEqual(kinds, [("GET", "query", "tokens"), ("POST", "login", None),
                                 ("GET", "cargoquery", None), ("GET", "cargoquery", None)])
        login = self.calls[1][1]
        self.assertEqual((login["lgname"], login["lgpassword"], login["lgtoken"]),
                         (self.USER, self.PASSWORD, "tok+\\"))
        self.assertTrue(c.logged_in)
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(self.USER, logs)
        self.assertNotIn(self.PASSWORD, logs)
        for f in pl.CACHE_DIR.iterdir():
            text = f.read_text()
            self.assertNotIn(self.PASSWORD, text)
            self.assertNotIn(self.USER, text)

    def test_failed_login_does_not_print_credentials(self):
        c, _, _ = self.client({pl.ENV_USER: self.USER, pl.ENV_PASSWORD: self.PASSWORD}, "Failed")
        with self.assertRaises(pl.CargoError) as ctx:
            self.run_query(c)
        self.assertIn("result: Failed", str(ctx.exception))
        self.assertNotIn(self.USER, str(ctx.exception))
        self.assertNotIn(self.PASSWORD, str(ctx.exception))

    def test_anonymous_when_unset_warns_once(self):
        c, _, err = self.client({pl.ENV_USER: self.USER})
        self.assertEqual(err.getvalue().count("warning"), 1)
        self.assertIn(pl.ENV_PASSWORD, err.getvalue())
        self.assertNotIn(self.USER, err.getvalue())
        _, logs = self.run_query(c)
        self.assertNotIn("warning", logs)
        self.assertEqual([p.get("action") for _, p in self.calls], ["cargoquery", "cargoquery"])

    def test_cached_calls_skip_login(self):
        env = {pl.ENV_USER: self.USER, pl.ENV_PASSWORD: self.PASSWORD}
        self.run_query(self.client(env)[0])
        self.calls.clear()
        c, _, _ = self.client(env)
        self.run_query(c)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
