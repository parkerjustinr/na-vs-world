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
        with contextlib.redirect_stdout(io.StringIO()):
            pl.pull(pl.FixtureClient(FIXTURES), cls.out)
        cls.matches = json.loads((cls.out / "matches.json").read_text())
        cls.tournaments = json.loads((cls.out / "tournaments.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_tournament_selection(self):
        included = {t["overview_page"] for t in self.tournaments["included"]}
        excluded = {t["overview_page"]: t["reason"] for t in self.tournaments["excluded"]}
        self.assertIn("Season 1 World Championship", included)
        self.assertIn("2024 Season World Championship/Play-In", included)
        self.assertIn("2025 First Stand", included)
        self.assertEqual(excluded["2023 Season World Championship/Qualifying Series"], "qualifier")
        self.assertEqual(excluded["Esports World Cup 2024"], "Esports World Cup")
        self.assertEqual(excluded["2015 All-Star Event"], "All-Stars")
        self.assertEqual(excluded["Rift Rivals/2019 Season/NA-EU"], "Rift Rivals")

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

    def test_ambiguous_region_resolved_by_league(self):
        fs = [s for s in self.matches["series"] if s["tournament"] == "2025 First Stand"]
        self.assertEqual({s["na_team"] for s in fs}, {"Team Liquid"})
        self.assertIn("Vivo Keyd Stars", {s["opponent"] for s in fs})

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

    def test_report_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pl.report(self.out)
        self.assertIn("Total NA games: 7-7", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
