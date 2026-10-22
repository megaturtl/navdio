"""Validated events, deterministic session selection and public snapshots."""

import base64
import binascii
import io
import json
import math
import threading
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import RadioError, json_object, library_path, reject_constant

MAX_BODY_BYTES = 64 * 1024
MAX_COVER_BYTES = 32 * 1024
MAX_SESSIONS = 256
LIVENESS_SECONDS = 15
PAUSED_SECONDS = 30 * 60


class EventError(RadioError):
    pass


def text(value, *, required=False, limit=4096):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value):
        raise ValueError
    value.encode("utf-8")
    return value


def number(value, low=0, high=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError
    return value


def cover_jpeg(encoded):
    if encoded is None:
        return None
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_COVER_BYTES + 2) // 3):
        raise ValueError
    source = base64.b64decode(encoded, validate=True)
    if not source or len(source) > MAX_COVER_BYTES:
        raise ValueError
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(source)) as image:
            if (
                image.format not in {"JPEG", "PNG", "WEBP"}
                or max(image.size) > 2048
                or image.width * image.height > 1024 * 1024
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError
            image.verify()
        with Image.open(io.BytesIO(source)) as image:
            thumbnail = ImageOps.fit(
                ImageOps.exif_transpose(image).convert("RGB"),
                (50, 50),
                method=Image.Resampling.LANCZOS,
            )
            output = io.BytesIO()
            thumbnail.save(output, format="JPEG", quality=85)
            return output.getvalue()


@dataclass(frozen=True)
class Track:
    key: tuple
    path: Path
    title: str | None
    artists: tuple[str, ...]
    album: str | None
    duration_ms: int
    cover: bytes | None = None


@dataclass(frozen=True)
class Observation:
    state: str = "idle"
    position_ms: int = 0
    observed_at: float = 0
    track: Track | None = None
    rate: float = 1

    def position(self, now):
        if self.track is None:
            return 0
        position = self.position_ms
        if self.state == "playing":
            position += max(0, now - self.observed_at) * 1000 * self.rate
        return int(min(position, self.track.duration_ms))

    def info(self, now):
        track = self.track
        return {
            "state": self.state if track else "idle",
            "title": track.title if track else None,
            "artists": list(track.artists) if track else [],
            "album": track.album if track else None,
            "positionMs": self.position(now),
            "durationMs": track.duration_ms if track else 0,
        }


@dataclass(frozen=True)
class PlaybackEvent:
    key: tuple[str, str]
    timestamp: float
    state: str
    position_ms: int
    rate: float
    track: Track | None


@dataclass(frozen=True)
class Heartbeat:
    pass


def parse_event(body, libraries):
    if len(body) > MAX_BODY_BYTES:
        raise EventError("Event exceeds 64 KiB")
    try:
        data = json.loads(body, object_pairs_hook=json_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise EventError("Invalid event JSON") from None
    try:
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise ValueError
        if data.get("type") == "heartbeat":
            if set(data) != {"version", "type"}:
                raise ValueError
            return Heartbeat()
        if data.get("type") != "playback" or set(data) - {
            "version",
            "type",
            "username",
            "playerId",
            "playerName",
            "state",
            "positionMs",
            "playbackRate",
            "timestamp",
            "track",
            "coverArt",
        }:
            raise ValueError
        username = text(data.get("username"), required=True, limit=1024)
        player_id = text(data.get("playerId"), required=True, limit=1024)
        text(data.get("playerName"), limit=1024)
        state = data.get("state")
        if state not in ("starting", "playing", "paused", "stopped", "expired"):
            raise ValueError
        timestamp = number(data.get("timestamp"))
        position = data.get("positionMs")
        if type(position) is not int or not 0 <= position <= 10**12:
            raise ValueError
        rate = number(data.get("playbackRate"), 0, math.inf)
        if rate == 0 and state in ("playing", "starting"):
            raise ValueError
        rate = rate or 1
        cover = cover_jpeg(data.get("coverArt"))
        track = None
        if state not in ("stopped", "expired"):
            song = data.get("track")
            if not isinstance(song, dict):
                raise ValueError
            track_id = text(song.get("id"), required=True, limit=1024)
            title = text(song.get("title"))
            album = text(song.get("album"))
            artists = song.get("artists")
            if artists is not None and (not isinstance(artists, list) or len(artists) > 128):
                raise ValueError
            names = []
            for artist in artists or []:
                if not isinstance(artist, dict):
                    raise ValueError
                name = text(artist.get("name"), limit=1024)
                if name:
                    names.append(name)
            legacy = text(song.get("artist"))
            if not names and legacy:
                names.append(legacy)
            duration = int(number(song.get("duration"), 0, 10**9) * 1000)
            path = library_path(libraries, song.get("libraryId"), song.get("path"))
            track = Track(
                (username, player_id, track_id, str(path)),
                path,
                title,
                tuple(names),
                album,
                duration,
                cover,
            )
            position = min(position, duration)
        return PlaybackEvent((username, player_id), timestamp, state, position, rate, track)
    except (
        ValueError,
        TypeError,
        OverflowError,
        binascii.Error,
        OSError,
        EOFError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        RadioError,
    ):
        raise EventError("Invalid playback event") from None


@dataclass(frozen=True)
class Session:
    event: PlaybackEvent
    received_at: float
    expires_at: float

    def observation(self):
        event = self.event
        # MPD cannot change playback speed. Silence sources with a different rate.
        if not math.isclose(event.rate, 1.0, rel_tol=1e-6):
            return Observation()
        return Observation(
            "playing" if event.state == "playing" else "paused",
            event.position_ms,
            self.received_at,
            event.track,
            event.rate,
        )


class RadioState:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.sessions: dict[tuple[str, str], Session] = {}
        self.last_event = None
        self.revision = 0
        self.rendered = Observation()

    def _expire(self, now):
        if self.last_event is not None and now - self.last_event >= LIVENESS_SECONDS:
            # A heartbeat after an outage must not revive old audio or cover art.
            self.sessions = {
                key: Session(replace(session.event, state="expired", track=None), now, now)
                for key, session in self.sessions.items()
            }
            self.last_event = None
            self.revision += 1
            self.rendered = Observation()
        self.sessions = {
            key: session
            for key, session in self.sessions.items()
            if now < session.expires_at + PAUSED_SECONDS
        }

    def accept(self, event):
        with self.lock:
            now = self.clock()
            self._expire(now)
            if isinstance(event, Heartbeat):
                self.last_event = now
            else:
                previous = self.sessions.get(event.key)
                if previous is not None and event.timestamp < previous.event.timestamp:
                    self.last_event = now
                    return
                if previous is None and len(self.sessions) >= MAX_SESSIONS:
                    raise EventError("Too many playback sessions")
                if event.state == "paused":
                    ttl = PAUSED_SECONDS
                elif event.track is not None:
                    ttl = (
                        max(0, event.track.duration_ms - event.position_ms) / 1000
                    ) / event.rate + 5
                else:
                    ttl = 0
                self.sessions[event.key] = Session(event, now, now + ttl)
                self.last_event = now
                self.revision += 1
        self.wake.set()

    def _selected(self, now):
        if self.last_event is None:
            return Observation()
        eligible = [
            session
            for session in self.sessions.values()
            if now < session.expires_at and session.event.track is not None
        ]
        if not eligible:
            return Observation()
        selected = min(
            eligible,
            key=lambda session: (
                0 if session.event.state == "playing" else 1,
                -session.event.timestamp,
                session.event.key[1],
                session.event.key[0],
            ),
        )
        return selected.observation()

    def desired(self):
        with self.lock:
            now = self.clock()
            self._expire(now)
            return self._selected(now), self.revision

    def publish(self, observation, revision):
        with self.lock:
            if revision == self.revision:
                self.rendered = observation

    def _public(self, now):
        self._expire(now)
        selected = self._selected(now)
        if (
            selected.track is None
            or self.rendered.track is None
            or selected.track.key != self.rendered.track.key
            or selected.state != self.rendered.state
        ):
            return Observation()
        return self.rendered

    def info(self):
        with self.lock:
            now = self.clock()
            return self._public(now).info(now)

    def cover(self):
        with self.lock:
            observation = self._public(self.clock())
            return observation.track.cover if observation.track else None

    def clear(self):
        with self.lock:
            self.sessions.clear()
            self.last_event = None
            self.rendered = Observation()
            self.revision += 1
        self.wake.set()
