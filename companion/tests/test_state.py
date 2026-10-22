import base64
import io
import json
import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import GifImagePlugin, Image, PngImagePlugin, XbmImagePlugin

from navdio.config import RadioError, library_path
from navdio.state import EventError, Heartbeat, RadioState, parse_event


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def artwork(size=(91, 63), format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format=format)
    return base64.b64encode(output.getvalue()).decode("ascii")


def apng(size, *, frames=2, disposal=1):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    source = b"\x89PNG\r\n\x1a\n"
    source += chunk(b"IHDR", struct.pack(">IIBBBBB", *size, 8, 6, 0, 0, 0))
    source += chunk(b"acTL", struct.pack(">II", frames, 0))
    source += chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 1, 1, 0, 0, 1, 10, disposal, 0))
    source += chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0"))
    source += chunk(b"IEND", b"")
    return base64.b64encode(source).decode("ascii")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.song = self.root / "song #100%.wav"
        self.song.write_bytes(b"test fixture")
        self.libraries = {"1": self.root}
        self.clock = Clock()
        self.state = RadioState(self.clock)

    def payload(self, **changes):
        result = {
            "version": 1,
            "type": "playback",
            "username": "private-user",
            "playerId": "b",
            "playerName": "private-player",
            "state": "playing",
            "positionMs": 1000,
            "playbackRate": 1.0,
            "timestamp": 1000,
            "track": {
                "id": "private-track",
                "title": "Title",
                "album": "Album",
                "artist": "Legacy",
                "artists": [{"name": "First", "id": "a"}, {"name": "Second"}],
                "duration": 120,
                "libraryId": 1,
                "path": self.song.name,
            },
            "coverArt": None,
        }
        result.update(changes)
        return result

    def event(self, **changes):
        return parse_event(json.dumps(self.payload(**changes)).encode(), self.libraries)

    def render(self):
        desired, revision = self.state.desired()
        self.state.publish(desired, revision)
        return desired

    def test_multi_player_selection_is_order_independent(self):
        for order in (("b", "a"), ("a", "b")):
            state = RadioState(self.clock)
            for player in order:
                state.accept(self.event(playerId=player))
            desired, _ = state.desired()
            self.assertEqual(desired.track.key[1], "a")
        self.state.accept(self.event(playerId="a", timestamp=2000, state="paused"))
        self.state.accept(self.event(playerId="b", timestamp=1000))
        self.assertEqual(self.state.desired()[0].track.key[1], "b")
        self.state.accept(self.event(playerId="c", timestamp=1001))
        self.assertEqual(self.state.desired()[0].track.key[1], "c")

    def test_backward_timestamp_cannot_rewind_or_revive(self):
        self.state.accept(self.event(timestamp=1002, positionMs=5000))
        self.state.accept(self.event(timestamp=1001, positionMs=0))
        self.assertEqual(self.state.desired()[0].position_ms, 5000)
        self.state.accept(self.event(timestamp=1003, state="stopped", track=None))
        self.state.accept(self.event(timestamp=1002))
        self.assertIsNone(self.state.desired()[0].track)

    def test_heartbeat_survives_sparse_reports_but_not_outage(self):
        self.state.accept(self.event(coverArt=artwork()))
        self.render()
        for _ in range(8):
            self.clock.now += 10
            self.state.accept(Heartbeat())
        self.assertEqual(self.state.info()["state"], "playing")
        self.assertIsNotNone(self.state.cover())
        self.clock.now += 15
        self.assertEqual(self.state.info()["state"], "idle")
        self.assertIsNone(self.state.cover())
        self.state.accept(Heartbeat())
        self.assertIsNone(self.state.desired()[0].track)
        self.state.accept(self.event(timestamp=1001))
        self.assertIsNotNone(self.state.desired()[0].track)

    def test_heartbeat_does_not_extend_track_or_pause_expiry(self):
        self.state.accept(self.event(positionMs=119000))
        for _ in range(6):
            self.clock.now += 1
            self.state.accept(Heartbeat())
        self.assertIsNone(self.state.desired()[0].track)
        self.state.accept(self.event(state="paused", timestamp=1001))
        for _ in range(179):
            self.clock.now += 10
            self.state.accept(Heartbeat())
        self.assertEqual(self.state.desired()[0].state, "paused")
        self.clock.now += 10
        self.state.accept(Heartbeat())
        self.assertIsNone(self.state.desired()[0].track)

    def test_cover_is_exact_jpeg_and_public_snapshot_has_no_identifiers(self):
        self.state.accept(self.event(coverArt=artwork()))
        self.render()
        self.clock.now += 2
        info = self.state.info()
        self.assertEqual(
            info,
            {
                "state": "playing",
                "title": "Title",
                "artists": ["First", "Second"],
                "album": "Album",
                "positionMs": 3000,
                "durationMs": 120000,
            },
        )
        with Image.open(io.BytesIO(self.state.cover())) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (50, 50))
        self.assertNotIn("private", json.dumps(info))
        self.assertNotIn(str(self.root), json.dumps(info))

    def test_single_image_formats_produce_50_pixel_jpeg(self):
        for format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(format=format):
                event = self.event(coverArt=artwork(format=format))
                with Image.open(io.BytesIO(event.track.cover)) as image:
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.size, (50, 50))
                    image.load()

    def test_unsupported_formats_do_not_enter_their_parsers(self):
        xbm = b""
        for prop in (b"width", b"height", b"x_hot", b"y_hot"):
            xbm += b"#define" + b" " * 60 + b"_" + prop + b" 1\r"
        xbm += b"\r" * 60 + b"_bits"
        covers = (base64.b64encode(xbm).decode("ascii"), artwork(format="GIF"))
        with (
            patch.object(
                XbmImagePlugin.XbmImageFile, "_open", side_effect=AssertionError("XBM parser")
            ) as xbm_open,
            patch.object(
                GifImagePlugin.GifImageFile, "_open", side_effect=AssertionError("GIF parser")
            ) as gif_open,
        ):
            for cover in covers:
                with self.subTest(cover=cover[:20]), self.assertRaises(EventError):
                    self.event(coverArt=cover)
            xbm_open.assert_not_called()
            gif_open.assert_not_called()

    def test_apng_rejected_before_canvas_allocation_or_decode(self):
        for size, frames in (((1025, 1024), 1), ((2049, 1), 1), ((64, 64), 2)):
            for disposal in (0, 1, 2):
                with (
                    self.subTest(size=size, frames=frames, disposal=disposal),
                    patch.object(
                        Image.core, "fill", side_effect=AssertionError("canvas allocation")
                    ) as fill,
                    patch.object(
                        PngImagePlugin.PngImageFile,
                        "load",
                        side_effect=AssertionError("image decode"),
                    ) as load,
                    self.assertRaises(EventError),
                ):
                    self.event(coverArt=apng(size, frames=frames, disposal=disposal))
                fill.assert_not_called()
                load.assert_not_called()

    def test_malformed_cover_cannot_mutate_existing_state(self):
        self.state.accept(self.event(coverArt=artwork()))
        self.render()
        old = self.state.info(), self.state.cover(), self.state.desired(), self.state.revision
        covers = (
            "not-base64",
            "",
            artwork((2049, 1)),
            artwork(format="GIF"),
            apng((1025, 1024)),
            apng((2049, 1)),
            apng((64, 64)),
        )
        for cover in covers:
            with self.subTest(cover=cover[:20]):
                with self.assertRaises(EventError):
                    self.state.accept(self.event(timestamp=2000, coverArt=cover))
                self.assertEqual(
                    (
                        self.state.info(),
                        self.state.cover(),
                        self.state.desired(),
                        self.state.revision,
                    ),
                    old,
                )

    def test_null_artists_fall_back_and_starting_is_silent(self):
        payload = self.payload(state="starting", playbackRate=1)
        payload["track"]["artists"] = None
        self.state.accept(parse_event(json.dumps(payload).encode(), self.libraries))
        self.assertEqual(self.render().state, "paused")
        self.assertEqual(self.state.info()["artists"], ["Legacy"])
        self.state.accept(self.event(timestamp=1001, state="paused", playbackRate=0))
        self.assertEqual(self.state.desired()[0].state, "paused")

    def test_nonunit_rate_silences_old_audio_without_selecting_older_player(self):
        self.state.accept(self.event(playerId="a", timestamp=999))
        self.state.accept(self.event())
        self.render()
        self.state.accept(self.event(timestamp=1001, playbackRate=2))
        self.assertIsNone(self.state.desired()[0].track)
        self.assertEqual(self.state.info()["state"], "idle")
        self.state.accept(self.event(timestamp=1002))
        self.assertEqual(self.state.desired()[0].state, "playing")

    def test_normal_speed_tolerates_client_floating_point_rounding(self):
        self.state.accept(self.event(playbackRate=1.0000000000000002))
        self.assertEqual(self.state.desired()[0].state, "playing")

    def test_old_control_snapshot_cannot_overwrite_newer_selection(self):
        self.state.accept(self.event())
        old, revision = self.state.desired()
        self.state.accept(self.event(state="stopped", timestamp=1001, track=None))
        self.state.publish(old, revision)
        self.assertEqual(self.state.info()["state"], "idle")

    def test_strict_json_and_numbers(self):
        invalid = [b'{"version":1,"version":1,"type":"heartbeat"}', b'{"version":NaN}', b"[]"]
        for body in invalid:
            with self.assertRaises(EventError):
                parse_event(body, self.libraries)
        for changes in (
            {"positionMs": True},
            {"timestamp": -1},
            {"playbackRate": 0},
            {"version": True},
        ):
            with self.assertRaises(EventError):
                self.event(**changes)
        payload = self.payload()
        payload["track"]["title"] = "\ud800"
        with self.assertRaises(EventError):
            parse_event(json.dumps(payload).encode(), self.libraries)

    def test_library_paths_are_literal_relative_and_symlink_confined(self):
        self.assertEqual(library_path(self.libraries, 1, self.song.name), self.song)
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_bytes(b"outside")
        self.addCleanup(outside.unlink)
        (self.root / "escape").symlink_to(outside)
        for path in (
            str(self.song),
            "../" + outside.name,
            "escape",
            "bad\nname",
            "bad\rname",
            "missing",
        ):
            with self.subTest(path=path), self.assertRaises(RadioError):
                library_path(self.libraries, 1, path)
        with self.assertRaises(RadioError):
            library_path(self.libraries, 2, self.song.name)

    def test_session_memory_is_bounded(self):
        from navdio.state import MAX_SESSIONS

        event = self.event()
        for index in range(MAX_SESSIONS):
            self.state.accept(replace(event, key=("user", str(index))))
        with self.assertRaises(EventError):
            self.state.accept(replace(event, key=("user", "overflow")))


if __name__ == "__main__":
    unittest.main()
