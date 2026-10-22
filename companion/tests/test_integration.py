"""Exercise the installed WASM, supported CLI grants, MPD, and public HTTP contract."""

import array
import io
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

PROJECT = Path(__file__).resolve().parent.parent
COMPANION = os.environ.get("NAVDIO_COMPANION_EXE")
PLUGIN = os.environ.get("NAVDIO_PLUGIN_ARCHIVE")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(url, data=None, headers=None):
    with urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers or {}), timeout=10
    ) as response:
        return response.read()


def eventually(predicate, label, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.15)
    raise AssertionError(f"Timed out waiting for {label}")


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@unittest.skipUnless(COMPANION and PLUGIN, "Set NAVDIO_COMPANION_EXE and NAVDIO_PLUGIN_ARCHIVE")
class IntegrationTests(unittest.TestCase):
    def test_native_plugin_permissions_and_shared_radio_lifecycle(self):
        companion, plugin = COMPANION, PLUGIN
        assert companion is not None and plugin is not None
        navidrome = shutil.which("navidrome")
        assert navidrome is not None, "Navidrome must be available in PATH"
        with tempfile.TemporaryDirectory(prefix="navdio-integration-") as temporary:
            root = Path(temporary)
            processes: list[subprocess.Popen[bytes]] = []
            logs: list[io.TextIOWrapper] = []
            token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            try:
                music, plugins = root / "music", root / "plugins"
                music.mkdir()
                plugins.mkdir()
                shutil.copyfile(plugin, plugins / "navdio.ndp")
                Image.new("RGB", (120, 80), (40, 160, 220)).save(music / "cover.jpg")
                for number in (1, 2):
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-f",
                            "lavfi",
                            "-i",
                            f"sine=frequency={number * 440}:sample_rate=48000:duration=90",
                            "-c:a",
                            "flac",
                            "-metadata",
                            f"title=Navdio integration {number}",
                            "-metadata",
                            "artist=Integration artist",
                            "-metadata",
                            "album=Integration album",
                            "-metadata",
                            "replaygain_track_gain=-6 dB",
                            "-metadata",
                            "replaygain_track_peak=0.5",
                            str(music / f"track {number} 50% # café.flac"),
                        ],
                        check=True,
                        capture_output=True,
                    )
                ports = set()
                while len(ports) < 3:
                    ports.add(free_port())
                port, source_port, nd_port = ports
                origin, nd_origin = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{nd_port}"
                credentials = root / "credentials"
                credentials.mkdir(mode=0o700)
                token_file = credentials / "event-token"
                token_file.write_text(token)
                token_file.chmod(0o600)
                config, nd_config, plugin_config = (
                    root / name for name in ("navdio.json", "navidrome.json", "plugin.json")
                )
                config.write_text(
                    json.dumps(
                        {
                            "port": port,
                            "sourcePort": source_port,
                            "libraries": {"1": str(music)},
                            "tokenFile": str(token_file),
                            "runtimeDirectory": str(root / "runtime"),
                            "mpd": shutil.which("mpd"),
                            "maxListeners": 1,
                        }
                    )
                )
                nd_config.write_text(
                    json.dumps(
                        {
                            "Address": "127.0.0.1",
                            "Port": nd_port,
                            "DataFolder": str(root / "data"),
                            "MusicFolder": str(music),
                            "CacheFolder": str(root / "cache"),
                            "LogLevel": "error",
                            "ScanInterval": "0",
                            "EnableInsightsCollector": False,
                            "EnableGravatar": False,
                            "Agents": "",
                            "NoBanner": True,
                            "Plugins": {"Folder": str(plugins), "Enabled": True},
                        }
                    )
                )

                def launch(command: list[str], name: str) -> subprocess.Popen[bytes]:
                    log = (root / name).open("w+")
                    logs.append(log)
                    process = subprocess.Popen(
                        command, stdout=log, stderr=log, start_new_session=True
                    )
                    processes.append(process)
                    return process

                launch([companion, "--config", str(config)], "companion.log")
                eventually(lambda: request(origin + "/radio/info"), "companion startup")
                setup = launch([navidrome, "--configfile", str(nd_config)], "setup.log")
                eventually(lambda: request(nd_origin + "/ping"), "Navidrome startup")
                request(
                    nd_origin + "/auth/createAdmin",
                    json.dumps(
                        {
                            "username": "broadcaster",
                            "password": password,
                        }
                    ).encode(),
                    {"Content-Type": "application/json"},
                )

                def subsonic(endpoint, **parameters):
                    form = {
                        "u": "broadcaster",
                        "p": password,
                        "v": "1.16.1",
                        "c": "NavdioIntegration",
                        "f": "json",
                        **parameters,
                    }
                    response = json.loads(
                        request(
                            nd_origin + "/rest/" + endpoint, urllib.parse.urlencode(form).encode()
                        )
                    )["subsonic-response"]
                    self.assertEqual(response["status"], "ok")
                    return response

                songs = eventually(
                    lambda: (
                        subsonic("search3", query="Navdio integration", songCount=10)
                        .get("searchResult3", {})
                        .get("song")
                    ),
                    "library scan",
                    timeout=45,
                )
                self.assertEqual(len(songs), 2)
                songs.sort(key=lambda song: song["title"])
                stop(setup)
                plugin_config.write_text(
                    json.dumps(
                        {
                            "username": "broadcaster",
                            "player": "NavdioIntegration",
                            "companionUrl": origin,
                        }
                    )
                )
                subprocess.run(
                    [
                        "python",
                        str(PROJECT / "scripts/configure-plugin.py"),
                        navidrome,
                        str(nd_config),
                        str(plugin_config),
                        "broadcaster",
                        "[1]",
                    ],
                    env={**os.environ, "CREDENTIALS_DIRECTORY": str(credentials)},
                    check=True,
                )
                server = launch([navidrome, "--configfile", str(nd_config)], "server.log")
                eventually(lambda: request(nd_origin + "/ping"), "enabled-plugin startup")

                def report(song, state="playing", position=2000):
                    subsonic(
                        "reportPlayback",
                        mediaId=song["id"],
                        mediaType="song",
                        state=state,
                        positionMs=position,
                        playbackRate=1,
                        ignoreScrobble="true",
                    )

                def info():
                    return json.loads(request(origin + "/radio/info"))

                def reporting_client():
                    # Navidrome loads plugins after its HTTP listener becomes ready.
                    report(songs[0])
                    return info()["state"] == "playing"

                eventually(reporting_client, "authorised plugin playback event")
                public = info()
                self.assertEqual(public["title"], songs[0]["title"])
                self.assertEqual(
                    set(public),
                    {
                        "state",
                        "title",
                        "artists",
                        "album",
                        "positionMs",
                        "durationMs",
                    },
                )
                with Image.open(io.BytesIO(request(origin + "/radio/cover"))) as cover:
                    self.assertEqual((cover.size, cover.format), ((50, 50), "JPEG"))
                with urllib.request.urlopen(
                    urllib.request.Request(origin + "/radio/stream", headers={"Icy-MetaData": "1"}),
                    timeout=15,
                ) as stream:
                    self.assertEqual(stream.read(4), b"OggS")
                    with self.assertRaises(urllib.error.HTTPError) as limited:
                        request(origin + "/radio/stream")
                    with limited.exception:
                        self.assertEqual(limited.exception.code, 503)
                # MPD sends Ogg pages about once a second. Allow time to detect
                # the disconnected listener before the decoder connects.
                time.sleep(2)
                decoded = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        origin + "/radio/stream",
                        "-t",
                        "2",
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                pcm = array.array("h", decoded.stdout)
                rms = math.sqrt(sum(sample * sample for sample in pcm) / len(pcm))
                expected_rms = (4095 / math.sqrt(2)) * 10 ** (-6 / 20)
                self.assertGreater(rms, expected_rms * 0.75)
                self.assertLess(rms, expected_rms * 1.25)
                before_seek = info()["positionMs"]
                report(songs[0], position=60000)
                time.sleep(0.6)
                self.assertLess(info()["positionMs"], before_seek + 4000)
                report(songs[0], state="paused", position=6500)
                eventually(lambda: info()["state"] == "paused", "pause")
                report(songs[0], position=7000)
                eventually(lambda: info()["state"] == "playing", "resume")
                report(songs[1], position=3000)
                eventually(
                    lambda: info()["title"] == songs[1]["title"] and info()["state"] == "playing",
                    "track change",
                )
                time.sleep(16)
                self.assertEqual(
                    info()["state"],
                    "playing",
                    "scheduled heartbeat must survive sparse client reports",
                )
                stop(server)
                eventually(lambda: info()["state"] == "idle", "source-loss expiry", timeout=22)
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    request(origin + "/radio/cover")
                with missing.exception:
                    self.assertEqual(missing.exception.code, 404)
            except BaseException:
                for log in logs:
                    log.flush()
                    log.seek(0)
                    print(f"--- {Path(log.name).name} ---")
                    print(
                        log.read()[-4000:].replace(token, "[TOKEN]").replace(password, "[PASSWORD]")
                    )
                raise
            finally:
                for process in reversed(processes):
                    stop(process)
                for log in logs:
                    log.close()


if __name__ == "__main__":
    unittest.main()
