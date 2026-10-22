import json
import tempfile
import unittest
from pathlib import Path

from navdio.config import Config, RadioError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "token"
        self.token_file.write_text("test-only-token\n")
        self.config_file = self.root / "config.json"
        self.data = {
            "libraries": {"1": str(self.root)},
            "tokenFile": str(self.token_file),
            "runtimeDirectory": str(self.root / "runtime"),
        }

    def load(self, **changes):
        self.config_file.write_text(json.dumps({**self.data, **changes}))
        return Config.load(self.config_file)

    def test_credential_newline_and_repr_privacy(self):
        config = self.load()
        self.assertEqual(config.token, b"test-only-token")
        self.assertNotIn("test-only-token", repr(config))

    def test_explicit_ipv6_address(self):
        self.assertEqual(self.load(listenAddress="::1").listen_address, "::1")

    def test_invalid_config_is_credential_and_path_free(self):
        cases = [
            {"unknown": True},
            {"port": True},
            {"port": 0},
            {"port": 8751},
            {"sourcePort": 65536},
            {"libraries": {}},
            {"libraries": {"01": str(self.root)}},
            {"libraries": {"0": str(self.root)}},
            {"libraries": {"1": "/"}},
            {"libraries": {"1": "relative"}},
            {"tokenFile": "relative"},
            {"runtimeDirectory": "/"},
            {"runtimeDirectory": "relative"},
            {"runtimeDirectory": "/tmp/bad\npath"},
            {"listenAddress": "localhost"},
            {"listenAddress": "::1%lo"},
            {"bitrateKbps": 0},
            {"maxListeners": 0},
            {"replaygain": "none"},
            {"mpd": "mpd --arg"},
            {"mpd": "relative/bin/mpd"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(RadioError) as error:
                    self.load(**changes)
                self.assertNotIn(str(self.root), str(error.exception))
                self.assertNotIn("test-only-token", str(error.exception))

    def test_invalid_and_oversize_credentials(self):
        for token in (b"", b"with spaces", b"line\nbreak", b"a" * 4097, b"\x00", b"\xff"):
            self.token_file.write_bytes(token)
            with self.subTest(token=token[:15]), self.assertRaises(RadioError):
                self.load()

    def test_duplicate_and_oversize_json(self):
        for content in ('{"port":8750,"port":8751}', " " * 65537):
            self.config_file.write_text(content)
            with self.assertRaises(RadioError):
                Config.load(self.config_file)


if __name__ == "__main__":
    unittest.main()
