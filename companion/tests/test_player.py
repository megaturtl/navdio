import http.client
import os
import shutil
import socket
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

from navdio.config import Config
from navdio.player import Player, PlayerError, SyncPolicy
from navdio.state import Observation, Track


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.track = Track(
            ("user", "player", "song"), Path("/music/file.wav"), "Song", (), None, 120000
        )
        self.policy = SyncPolicy()

    def observation(self, state="playing", position=1000, now=100):
        return Observation(state, position, now, self.track)

    def test_initial_seek_resume_and_no_clock_correction(self):
        _, restart = self.policy.update(self.observation(), 0, 100)
        self.assertTrue(restart)
        actual, restart = self.policy.update(self.observation(position=50000), 2000, 101)
        self.assertFalse(restart)
        self.assertEqual(actual.position_ms, 2000)
        self.policy.update(self.observation("paused", 30000), 2000, 102)
        actual, restart = self.policy.update(self.observation(position=30000, now=103), 2000, 103)
        self.assertTrue(restart)
        self.assertEqual(actual.position_ms, 30000)

    def test_natural_repeat_and_short_track_windows(self):
        self.policy.update(self.observation(), 0, 100)
        _, restart = self.policy.update(self.observation(position=50, now=101), 119900, 101)
        self.assertTrue(restart)
        short = Track(("short",), Path("/music/short.wav"), None, (), None, 2000)
        initial = Observation("playing", 0, 100, short)
        self.policy.update(initial, 0, 100)
        _, restart = self.policy.update(initial, 250, 100.25)
        self.assertFalse(restart)


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.song = self.root / "literal #100% space.wav"
        self.song.write_bytes(b"fixture")
        self.config = Config(
            {"1": self.root}, b"test-only-token", runtime_directory=self.root / "runtime"
        )
        self.player = Player(self.config, clock=lambda: 100)
        self.client = MagicMock()
        self.client.status.return_value = {"state": "play", "elapsed": "0.5"}
        self.player.client = self.client
        self.process = MagicMock()
        self.process.poll.return_value = None
        self.player.process = self.process
        self.track = Track(("song",), self.song, None, (), None, 120000)

    def test_mpd_error_is_fixed_and_fails_closed(self):
        self.client.status.return_value = {"state": "play", "error": str(self.song)}
        with self.assertRaises(PlayerError) as error:
            self.player.apply(Observation("playing", 0, 100, self.track))
        self.assertNotIn(str(self.song), str(error.exception))
        self.client.add.assert_not_called()

    def test_runtime_files_are_private_and_single_instance(self):
        self.player.process = None
        self.addCleanup(self.player.close)
        config_path, _, _ = self.player._runtime()
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config_path.parent.stat().st_mode & 0o777, 0o700)
        second = Player(self.config)
        self.addCleanup(second.close)
        with self.assertRaises(PlayerError):
            second._runtime()

    def test_replacement_after_validation_cannot_change_the_opened_track(self):
        self.player._runtime()
        self.addCleanup(self.player.close)
        original = self.song.read_bytes()
        with tempfile.TemporaryDirectory() as private:
            outside = Path(private) / "private.wav"
            outside.write_bytes(b"outside-library audio")

            def replace_song():
                self.song.unlink()
                self.song.symlink_to(outside)

            decoded = []
            self.client.clear.side_effect = replace_song
            self.client.add.side_effect = lambda uri: decoded.append(
                Path(uri.removeprefix("file://")).read_bytes()
            )
            self.player.apply(Observation("playing", 0, 100, self.track))
            self.assertEqual(decoded, [original])
            self.assertEqual(self.song.read_bytes(), b"outside-library audio")
            source = self.player.source_file
            assert source is not None
            self.client.clear.side_effect = None
            self.player.apply(Observation())
            self.assertTrue(source.closed)

    def test_replacement_before_open_cannot_escape_the_library(self):
        with tempfile.TemporaryDirectory() as private:
            outside = Path(private) / "private.wav"
            outside.write_bytes(b"outside-library audio")
            self.song.unlink()
            self.song.symlink_to(outside)
            with self.assertRaises(PlayerError):
                self.player.apply(Observation("playing", 0, 100, self.track))
            self.client.add.assert_not_called()

    def test_nonregular_track_does_not_block_or_reach_mpd(self):
        self.song.unlink()
        os.mkfifo(self.song)
        with self.assertRaises(PlayerError):
            self.player.apply(Observation("playing", 0, 100, self.track))
        self.client.add.assert_not_called()


MPD = os.environ.get("NAVDIO_TEST_MPD") or shutil.which("mpd")


@unittest.skipUnless(MPD, "MPD unavailable: set NAVDIO_TEST_MPD or put mpd on PATH")
class RealMPDTests(unittest.TestCase):
    def test_native_opus_literal_uri_seek_and_reaped_child(self):
        assert MPD is not None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            music = root / "music"
            music.mkdir()
            song = music / "audio #100% literal.wav"
            with wave.open(str(song), "wb") as output:
                output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                output.writeframes(b"\x00\x10\x00\x10" * (48000 * 6))
            outside = root / "private.wav"
            with wave.open(str(outside), "wb") as output:
                output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                output.writeframes(b"\x00\x20\x00\x20" * 48000)
            config = Config(
                {"1": music},
                b"test-only-token",
                source_port=free_port(),
                runtime_directory=root / "runtime",
                mpd=Path(MPD).name,
            )
            player = Player(config)
            source = None
            try:
                with patch.dict(
                    os.environ,
                    {
                        "NAVDIO_TEST_CREDENTIAL": "test-only-value",
                        "PATH": str(Path(MPD).parent),
                    },
                ):
                    player.start()
                process = player.process
                assert process is not None
                self.assertIsNone(process.poll())
                environment = Path(f"/proc/{process.pid}/environ")
                if environment.exists():
                    self.assertNotIn(b"NAVDIO_TEST_CREDENTIAL", environment.read_bytes())
                source = http.client.HTTPConnection("127.0.0.1", config.source_port, timeout=5)
                source.request("GET", "/")
                response = source.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIsNone(response.getheader("icy-metaint"))
                data = bytearray()
                deadline = time.monotonic() + 5
                while b"OpusHead" not in data and time.monotonic() < deadline:
                    data.extend(response.read1(8192))
                self.assertTrue(data.startswith(b"OggS"))
                self.assertIn(b"OpusHead", data)
                track = Track(("song",), song, "Title", (), None, 6000)
                clear = player.client.clear

                def replace_song():
                    clear()
                    song.unlink()
                    song.symlink_to(outside)

                with patch.object(player.client, "clear", side_effect=replace_song):
                    player.apply(Observation("playing", 2000, time.monotonic(), track))
                status = player.status()
                self.assertEqual(status["state"], "play")
                self.assertAlmostEqual(float(status["duration"]), 6, delta=0.1)
                elapsed = float(status["elapsed"])
                self.assertGreaterEqual(elapsed, 1.5)
                self.assertLess(elapsed, 3)
            finally:
                if source is not None:
                    source.close()
                player.close()
            self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
