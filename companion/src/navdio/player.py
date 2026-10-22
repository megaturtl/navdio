"""One MPD process and one non-drifting playback policy."""

import fcntl
import math
import os
import shutil
import stat
import subprocess
import time
import wave
from pathlib import Path
from typing import BinaryIO

from mpd import MPDClient, MPDError

from .config import Config, RadioError
from .state import Observation


class PlayerError(RadioError):
    pass


def quoted(value):
    value = str(value)
    if any(char in value for char in "\r\n\x00"):
        raise PlayerError("Invalid private MPD path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SyncPolicy:
    """Seek on track changes, resume, and natural repeats."""

    def __init__(self):
        self.key = None
        self.state = "idle"

    def update(self, observation, position_ms, now):
        track = observation.track
        if track is None:
            self.key, self.state = None, "idle"
            return Observation(), False
        target = observation.position(now)
        repeat_window = min(3000, track.duration_ms / 4)
        restart = observation.state == "playing" and (
            self.key != track.key
            or self.state != "playing"
            or (
                repeat_window > 0
                and position_ms >= track.duration_ms - repeat_window
                and target < repeat_window
            )
        )
        self.key, self.state = track.key, observation.state
        position = target if restart or observation.state != "playing" else position_ms
        return Observation(observation.state, position, now, track), restart


class Player:
    def __init__(self, config: Config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.client = MPDClient()
        self.client.timeout = 3
        self.process: subprocess.Popen[bytes] | None = None
        self.lock_file: BinaryIO | None = None
        self.loaded_key = None
        self.policy = SyncPolicy()

    def _runtime(self):
        runtime = self.config.runtime_directory
        try:
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = runtime.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError
            runtime.chmod(0o700)
            fd = os.open(runtime / "instance.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self.lock_file = os.fdopen(fd, "wb")
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            empty = runtime / "empty"
            empty.mkdir(mode=0o700, exist_ok=True)
            if empty.is_symlink() or not empty.is_dir() or any(empty.iterdir()):
                raise OSError
            silence = runtime / "silence.wav"
            with silence.open("wb") as source:
                os.fchmod(source.fileno(), 0o600)
                with wave.open(source, "wb") as output:
                    output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                    output.writeframes(bytes(4800 * 4))
            socket = runtime / "mpd.sock"
            socket.unlink(missing_ok=True)
            config_path = runtime / "mpd.conf"
            content = f'''music_directory {quoted(empty)}
playlist_directory {quoted(runtime)}
bind_to_address {quoted(socket)}
auto_update "no"
zeroconf_enabled "no"
replaygain "{self.config.replaygain}"
replaygain_limit "yes"
audio_buffer_size "512"

audio_output {{
    type "httpd"
    name "navdio"
    bind_to_address "127.0.0.1"
    port "{self.config.source_port}"
    max_clients "{self.config.max_listeners}"
    format "48000:16:2"
    encoder "opus"
    bitrate "{self.config.bitrate_kbps * 1000}"
    vbr "constrained"
    opustags "no"
    always_on "yes"
    mixer_type "none"
}}
'''
            with config_path.open("w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
            return config_path, socket, silence
        except (OSError, ValueError):
            raise PlayerError("Cannot prepare private MPD runtime") from None

    def status(self):
        if self.process is None or self.process.poll() is not None:
            raise PlayerError("MPD stopped")
        try:
            status = self.client.status()
            elapsed = float(status.get("elapsed", 0))
            if (
                status.get("error")
                or status.get("state") not in {"play", "pause", "stop"}
                or not math.isfinite(elapsed)
                or elapsed < 0
            ):
                raise ValueError
            return status
        except (MPDError, OSError, ValueError, TypeError):
            raise PlayerError("MPD playback failed") from None

    def start(self, stopped=lambda: False):
        config_path, socket, silence = self._runtime()
        executable = shutil.which(self.config.mpd)
        if executable is None:
            raise PlayerError("Cannot locate private MPD executable")
        try:
            self.process = subprocess.Popen(
                [executable, "--no-daemon", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "PATH": os.defpath,
                    "HOME": str(self.config.runtime_directory),
                    "LANG": "C.UTF-8",
                },
            )
        except OSError:
            raise PlayerError("Cannot start private MPD") from None
        deadline = self.clock() + 10
        while not stopped():
            if self.process.poll() is not None:
                raise PlayerError("MPD stopped during startup")
            try:
                self.client.connect(str(socket))
                break
            except (MPDError, OSError):
                if self.clock() >= deadline:
                    raise PlayerError("MPD socket startup timed out") from None
                time.sleep(0.05)
        else:
            return
        try:
            self.client.clear()
            self.client.repeat(0)
            self.client.random(0)
            self.client.single(0)
            self.client.consume(0)
            self.client.add("file://" + str(silence))
            self.client.play()
            # Open the encoder once. always_on keeps the Ogg stream open at idle.
            deadline = self.clock() + 5
            while not stopped() and self.status()["state"] != "stop":
                if self.clock() >= deadline:
                    raise PlayerError("MPD silence bootstrap timed out")
                time.sleep(0.05)
            self.client.clear()
        except (MPDError, OSError):
            raise PlayerError("MPD bootstrap failed") from None

    def apply(self, observation):
        status = self.status()
        position = int(float(status.get("elapsed", 0)) * 1000)
        if (
            status["state"] == "stop"
            and observation.track
            and self.loaded_key == observation.track.key
        ):
            position = observation.track.duration_ms or position
        result, restart = self.policy.update(observation, position, self.clock())
        try:
            if result.track is None:
                if status["state"] != "stop" or self.loaded_key is not None:
                    self.client.stop()
                    self.client.clear()
                self.loaded_key = None
            elif result.state != "playing":
                if status["state"] == "play":
                    self.client.pause(1)
            elif restart:
                if self.loaded_key != result.track.key:
                    path = Path(result.track.path)
                    if (
                        not path.is_file()
                        or not any(
                            path.resolve(strict=True).is_relative_to(root)
                            for root in self.config.libraries.values()
                        )
                        or any(char in str(path) for char in "\r\n\x00")
                    ):
                        raise PlayerError("Track is no longer available in its library")
                    self.client.clear()
                    # MPD's local file scheme takes literal paths, not URI escapes.
                    self.client.add("file://" + str(path))
                    self.loaded_key = result.track.key
                self.client.seek(0, result.position_ms / 1000)
                self.client.pause(0)
            self.status()
            return result
        except (MPDError, OSError, ValueError, RuntimeError):
            raise PlayerError("MPD synchronisation failed") from None

    def close(self):
        try:
            self.client.disconnect()
        except (MPDError, OSError):
            pass
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                else:
                    self.process.wait()
        finally:
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None
