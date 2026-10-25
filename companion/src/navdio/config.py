"""Startup configuration and the private MPD runtime."""

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class RadioError(Exception):
    """Only fixed, credential-free messages may cross this boundary."""


def json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("Nonfinite JSON number")


def path_value(value):
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in "\r\n\x00")
        or not Path(value).is_absolute()
    ):
        raise ValueError
    return Path(value)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError
    return value


@dataclass(frozen=True)
class Config:
    libraries: dict[str, Path]
    token: bytes = field(repr=False)
    listen_address: str = "127.0.0.1"
    port: int = 8750
    source_port: int = 8751
    runtime_directory: Path = Path("/run/navdio")
    bitrate_kbps: int = 32
    replaygain: str = "track"
    max_listeners: int = 40
    mpd: str = "mpd"
    stats_database: Path = Path("/var/lib/navdio/stats.sqlite3")
    stats_trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def load(cls, filename):
        try:
            with open(filename, "rb") as source:
                raw = source.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError
            data = json.loads(raw, object_pairs_hook=json_object, parse_constant=reject_constant)
            if not isinstance(data, dict) or set(data) - {
                "listenAddress",
                "port",
                "sourcePort",
                "libraries",
                "tokenFile",
                "runtimeDirectory",
                "bitrateKbps",
                "replaygain",
                "maxListeners",
                "mpd",
                "statsDatabase",
                "statsTrustedProxies",
            }:
                raise ValueError
            address = data.get("listenAddress", "127.0.0.1")
            if not isinstance(address, str) or "%" in address:
                raise ValueError
            ipaddress.ip_address(address)
            port = integer(data.get("port", 8750), 1, 65535)
            source_port = integer(data.get("sourcePort", 8751), 1, 65535)
            if port == source_port:
                raise ValueError
            roots = data["libraries"]
            if not isinstance(roots, dict) or not roots:
                raise ValueError
            libraries = {}
            for key, value in roots.items():
                if not re.fullmatch(r"[1-9][0-9]*", key):
                    raise ValueError
                root = path_value(value).resolve(strict=True)
                if root == Path("/") or not root.is_dir():
                    raise ValueError
                libraries[key] = root
            token_path = path_value(data["tokenFile"])
            with token_path.open("rb") as source:
                token = source.read(4098)
            token = token.removesuffix(b"\n").removesuffix(b"\r")
            if not 1 <= len(token) <= 4096 or any(c < 33 or c > 126 for c in token):
                raise ValueError
            runtime = path_value(data.get("runtimeDirectory", "/run/navdio"))
            if len(os.fsencode(runtime / "mpd.sock")) > 100 or runtime == Path("/"):
                raise ValueError
            stats_database = path_value(data.get("statsDatabase", "/var/lib/navdio/stats.sqlite3"))
            if stats_database == Path("/"):
                raise ValueError
            proxies = data.get("statsTrustedProxies", [])
            if not isinstance(proxies, list) or any(
                not isinstance(value, str) for value in proxies
            ):
                raise ValueError
            stats_trusted_proxies = tuple(
                ipaddress.ip_network(value, strict=False) for value in proxies
            )
            bitrate = integer(data.get("bitrateKbps", 32), 6, 510)
            listeners = integer(data.get("maxListeners", 40), 1, 1024)
            replaygain = data.get("replaygain", "track")
            if replaygain not in ("track", "album", "off"):
                raise ValueError
            mpd = data.get("mpd", "mpd")
            if (
                not isinstance(mpd, str)
                or not mpd
                or any(ord(char) < 33 or ord(char) == 127 for char in mpd)
            ):
                raise ValueError
            if not Path(mpd).is_absolute() and not re.fullmatch(r"[A-Za-z0-9_.+-]+", mpd):
                raise ValueError
            return cls(
                libraries=libraries,
                token=token,
                listen_address=address,
                port=port,
                source_port=source_port,
                runtime_directory=runtime,
                bitrate_kbps=bitrate,
                replaygain=replaygain,
                max_listeners=listeners,
                mpd=mpd,
                stats_database=stats_database,
                stats_trusted_proxies=stats_trusted_proxies,
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, RecursionError):
            raise RadioError("Invalid or unavailable companion configuration") from None


def library_path(libraries, library_id, song_path):
    try:
        if type(library_id) is int and library_id > 0:
            library_id = str(library_id)
        if (
            not isinstance(library_id, str)
            or not isinstance(song_path, str)
            or not song_path
            or any(char in song_path for char in "\r\n\x00")
            or Path(song_path).is_absolute()
        ):
            raise ValueError
        root = libraries[library_id]
        result = (root / song_path).resolve(strict=True)
        if not result.is_relative_to(root) or not result.is_file():
            raise ValueError
        return result
    except (KeyError, OSError, ValueError, RuntimeError):
        raise RadioError("Track is not an accessible file in a configured library") from None
