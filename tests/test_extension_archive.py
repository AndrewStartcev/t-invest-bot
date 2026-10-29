"""The admin extension download must contain source files only."""
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from extension_archive import EXTENSION_FILES, ZIP_ROOT, extension_zip


class ArchiveTests(unittest.TestCase):
    def test_complete_unpacked_extension(self):
        root = Path(__file__).resolve().parents[1]
        data = extension_zip(root)
        with ZipFile(BytesIO(data)) as archive:
            paths = archive.namelist()
            expected = [ZIP_ROOT + "/" + filename for filename in EXTENSION_FILES]
            self.assertEqual(expected, paths)
            self.assertIn("manifest.json", paths[0])
            manifest = json.loads(archive.read(ZIP_ROOT + "/manifest.json"))
            self.assertEqual(3, manifest["manifest_version"])
            self.assertEqual("background.js", manifest["background"]["service_worker"])
            for name in paths:
                self.assertNotIn(".local", name)
                self.assertNotIn("token.txt", name)
                self.assertFalse(name.startswith("/"))

    def test_no_secrets_from_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "extension"
            folder.mkdir()
            for file in EXTENSION_FILES:
                (folder / file).write_text("test", encoding="utf-8")
            (root / ".local").mkdir()
            (root / ".local" / "broker-trade-token.txt").write_text("PRIVATE_TOKEN", encoding="utf-8")
            (folder / "pulse-session.json").write_text("BANK_COOKIE", encoding="utf-8")
            archive = extension_zip(root)
            self.assertNotIn(b"PRIVATE_TOKEN", archive)
            self.assertNotIn(b"BANK_COOKIE", archive)
            with ZipFile(BytesIO(archive)) as zipfile:
                self.assertEqual(len(EXTENSION_FILES), len(zipfile.namelist()))


if __name__ == "__main__":
    unittest.main()
