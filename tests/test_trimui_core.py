
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from trimui_core import (
    scan_library,
    asset_path_for_game,
    asset_destination,
    build_asset_index,
    folder_tag,
    inspect_card,
)


class NXReduxCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.roms = self.root / "Roms"
        self.psx = self.roms / "Sony PlayStation (PSX)"
        self.psx.mkdir(parents=True)
        (self.psx / "Crash.cue").write_text(
            "FILE Crash.bin BINARY\n", encoding="utf-8"
        )
        (self.psx / "Crash.bin").write_bytes(b"disc-track")
        (self.psx / "Legend.chd").write_bytes(b"game")

        art = self.psx / ".media" / "screenshot"
        art.mkdir(parents=True)
        Image.new("RGB", (64, 64), (20, 200, 140)).save(
            art / "Legend.png"
        )

        discs = self.psx / "Final Game"
        discs.mkdir()
        (discs / "Final Game.m3u").write_text(
            "disc1.cue\ndisc2.cue\n", encoding="utf-8"
        )
        (discs / "disc1.cue").write_text(
            "FILE d1.bin BINARY\n", encoding="utf-8"
        )
        (discs / "d1.bin").write_bytes(b"track-one")

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_groups_cue_bin_and_multidisc(self):
        games = scan_library(self.root)
        names = [g["name"] for g in games]
        self.assertIn("Crash", names)
        self.assertIn("Legend", names)
        self.assertIn("Final Game", names)
        self.assertNotIn("d1", names)
        self.assertEqual(names.count("Crash"), 1)
        self.assertEqual(names.count("Final Game"), 1)

    def test_artwork_location_matches_nx_redux(self):
        games = scan_library(self.root)
        legend = next(g for g in games if g["name"] == "Legend")
        expected = self.psx / ".media" / "screenshot" / "Legend.png"
        self.assertEqual(asset_path_for_game(legend), expected)
        self.assertEqual(asset_destination(legend), expected)

    def test_art_index_scans_resource_directories(self):
        fallback = self.psx / ".media" / "Crash.png"
        Image.new("RGB", (30, 40), (200, 100, 10)).save(fallback)
        games = scan_library(self.root)
        index = build_asset_index(games)
        crash = next(g for g in games if g["name"] == "Crash")
        legend = next(g for g in games if g["name"] == "Legend")
        self.assertEqual(
            index[(str(crash["path"]), "screenshot")], fallback
        )
        self.assertEqual(
            index[(str(legend["path"]), "screenshot")],
            self.psx / ".media" / "screenshot" / "Legend.png",
        )
        self.assertIsNone(index[(str(crash["path"]), "boxart")])

    def test_image_lookup_is_case_insensitive(self):
        art = self.psx / ".media" / "screenshot"
        Image.new("RGB", (32, 32), (90, 90, 90)).save(art / "CRASH.JPG")
        games = scan_library(self.root)
        crash = next(g for g in games if g["name"] == "Crash")
        self.assertEqual(asset_path_for_game(crash), art / "CRASH.JPG")

    def test_folder_tag_and_card_diagnostics(self):
        self.assertEqual(folder_tag("Sony PlayStation (PSX)"), "PSX")
        self.assertIsNone(folder_tag("PlayStation"))
        games = scan_library(self.root)
        data = inspect_card(self.root, games=games)
        self.assertTrue(data["checks"][0]["ok"])
        self.assertTrue(data["checks"][1]["ok"])
        self.assertEqual(data["screenshot_count"], 1)


if __name__ == "__main__":
    unittest.main()
