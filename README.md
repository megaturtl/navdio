# Navdio

Navdio broadcasts one Navidrome user's playback as a shared Opus stream, with track metadata and a 50×50 JPEG cover.
It plays library files on the server. It does not capture the user's audio output.

Install and configure Navdio through its NixOS module.

A Navidrome WebAssembly plugin sends playback reports to a native companion.
The companion controls a private MPD process for playback, ReplayGain, and Opus encoding.
The plugin sandbox cannot open HTTP listeners or start MPD, so the companion runs separately.

## Requirements

- NixOS and Navidrome 0.64.2 or newer. The plugin uses the 0.64.2 API.
- A client that sends state and position through the OpenSubsonic playback-report extension, such as a compatible Feishin version.
- Navidrome and the companion on the same machine, with read access to the same music files. Their library mount paths can differ.
- MPD with HTTP output and Opus support. The Nix package includes it.

Legacy now-playing and scrobble reports do not include the required pause and position data.
Navidrome's playback filters also affect which reports reach the plugin.
Other Subsonic servers are not supported.

## NixOS setup

Add the flake and import its module. For a local checkout:

```nix
{
  inputs.navdio = {
    url = "path:/absolute/path/to/navdio";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = {nixpkgs, navdio, ...}: {
    nixosConfigurations.music = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        navdio.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

Replace the path with your checkout or repository URL.
Use nixpkgs with `services.navidrome.plugins` and Navidrome 0.64.2 or newer.
Local path inputs use locked snapshots.
After source changes, run `nix flake update navdio` in the consuming flake.

Configure Navidrome and Navdio:

```nix
{
  services.navidrome = {
    enable = true;
    settings.MusicFolder = "/srv/music";
  };

  services.navdio = {
    enable = true;
    username = "alice";
    libraries."1" = "/srv/music";
    tokenFile = "/run/secrets/navdio-events";
    supplementaryGroups = ["music"];
  };
}
```

Create the selected user and libraries in Navidrome first.
Use the numeric IDs from Navidrome's library administration.
The `music` group must grant read and directory traversal access to the library.
Navdio does not change library permissions.
Its service uses `ProtectHome=true`, so keep music outside private home directories.
The companion runs as `navdio`. Groups assigned only to the `navidrome` user do not grant it access.

Supply a high-entropy event token through your secret manager.
The `tokenFile` must exist before startup and must not be in `/nix/store`.
With sops-nix, use the secret's `.path`, not a token string.
The module passes the file to both services through systemd credentials.

After token rotation, restart `navdio.service` and `navidrome.service` to reload the token and update the plugin.
With sops-nix, add both services to the secret's `restartUnits`.

### Managed services

The module:

- Installs the plugin through `services.navidrome.plugins`.
- Starts the companion and MPD with read-only library access.
- Configures and enables the plugin through Navidrome's CLI before Navidrome starts.
- Grants access only to the selected user and libraries, without library writes.
- Sends the token through stdin, not command-line arguments.
- Restarts Navidrome when managed plugin settings change.

If provisioning fails, Navidrome can still start.
Fix the prerequisites, then restart Navidrome to apply the plugin settings.
The module does not create Navidrome users, change authentication, open firewall ports, or configure a public proxy.

Set `services.navdio.configurePlugin = false` to manage plugin settings and grants yourself.
Otherwise, each Navidrome startup restores the managed values. Web UI edits to those settings do not persist.

### Options

| Option | Purpose or default |
| --- | --- |
| `username` | Required broadcast user |
| `player` | Case-insensitive client-name filter. Empty permits all eligible clients. |
| `libraries` | Required mapping of library IDs to read-only music roots |
| `tokenFile` | Required runtime event-token file |
| `listenAddress` | Companion bind address. Default: `127.0.0.1` |
| `port` | Companion HTTP port. Default: 8750 |
| `sourcePort` | Private MPD loopback port. Default: 8751 |
| `bitrateKbps` | Opus constrained-VBR target. Default: 32 |
| `replaygain` | `track`, `album`, or `off`, with peak limiting. Default: `track` |
| `maxListeners` | Concurrent direct stream connections. Default: 40 |
| `supplementaryGroups` | Existing groups needed to read music |
| `configurePlugin` | Manage plugin settings and grants. Default: true |

Do not expose the companion's entire HTTP port. It includes the private event endpoint.
Use a reverse proxy for only the three public GET routes.
Each listener uses one companion stream connection, including through nginx.
Bitrate and listener limits give an approximate bandwidth budget, not a strict traffic cap.

## Playback and endpoints

| Endpoint | Response |
| --- | --- |
| `GET /radio/stream` | Shared Ogg/Opus audio |
| `GET /radio/info` | State, track metadata, position, and duration |
| `GET /radio/cover` | 50×50 JPEG, or 404 |

Fetch artwork from `/radio/cover` directly. Info does not include a cover URL.

Track changes, resume, and natural repeats start at the reported position.
Mid-song seeks, clock drift, and new listeners do not reposition the shared player.
Client reports and audio buffers make synchronisation approximate.

Pause, idle, and source loss produce compressed silence without disconnecting listeners.
Idle traffic is small, not zero.
Without listeners, Navdio sends no audio, but MPD's encoder can remain active.
A plugin heartbeat detects source loss without requiring frequent client reports.
When source liveness expires, metadata becomes idle and the cover disappears.

MPD reads ReplayGain tags from the files. Missing tags leave gain unchanged.
Navdio does not submit plays or scrobbles.
Public responses exclude tokens, native paths, and user or player identifiers.

## Check the installation

1. Request `http://127.0.0.1:8750/radio/info` on the Navidrome host. The root path `/` returns 404.
2. Play a library track with the selected user and a compatible client.
3. Check the metadata, then listen to `/radio/stream`.
4. Check `/radio/cover` for a 50×50 JPEG when artwork exists.
5. Pause, resume, and change tracks. A second listener must not restart playback.

Navidrome loads plugins asynchronously. The radio waits for a new playback report after the plugin loads.
Reports from before startup are not replayed.
Navidrome's **Report Real Path** setting is not needed. The plugin sends library-relative paths.

If info stays idle, check `navdio.service`, `navdio-plugin.service`, and `navidrome.service` logs.
Check the selected user, client playback-report support, library grants, and Navidrome's playback filters.
Do not widen permissions to diagnose failures.

Navidrome 0.64.2 checks plugin grants against internal user IDs, despite its CLI help asking for usernames.
The provisioning helper resolves the ID automatically.
If you manage grants manually, use the internal ID and restart Navidrome after offline CLI changes.

## Documentation

- [Public proxy](docs/proxy.md): nginx routes, remote ingress, and audience limits.
- [Protocol](docs/protocol.md): private events and public metadata.

Navidrome stores the plugin token in its database. Protect the database and its backups.
The companion reads the token from its protected file.

## Development

```sh
nix develop
nix fmt
ty check
python -B -m unittest discover -s companion/tests -v
go -C plugin test ./...
go -C plugin vet ./...
nix flake check
nix build .#navdio .#plugin
```

The shell includes the compilers, runtime dependencies, and check tools.
It sets `PYTHONPATH` to include `companion/src`, so `python -m navdio` works without a virtualenv.
Start your editor from this shell.

Treefmt uses Alejandra, gofmt, and Ruff. Use the same tools for editor formatting.
`nix flake check` checks formatting without changing the checkout and runs ty over Python source, tests, and scripts.
`companion/typings/mpd.pyi` describes the dynamic python-mpd2 methods used here. It does not replace the runtime dependency.
Update `flake.lock` and `plugin/go.sum` through Nix and Go, not by hand.

The flake checks the full Navidrome → plugin → companion → MPD path with generated music and test credentials.
To run only that check:

```sh
nix build .#checks.x86_64-linux.integration
```

Python test discovery skips this check unless `NAVDIO_COMPANION_EXE` and `NAVDIO_PLUGIN_ARCHIVE` point to built artifacts.

### Layout

| Path | Purpose |
| --- | --- |
| `companion/src/navdio/` | Session selection, MPD control, artwork conversion, and HTTP routes |
| `companion/tests/` | Python tests and integration checks |
| `companion/scripts/configure-plugin.py` | Plugin settings and grants through Navidrome's CLI |
| `plugin/` | Navidrome SDK adapter, playback reports, artwork cache, and heartbeats |
| `nix/packages/` | Companion and plugin packages |
| `nix/modules/navdio.nix` | NixOS options, credentials, and services |
| `nix/tests/` | Module assertions and generated service checks |

The flake exports `packages.<system>.navdio`, `packages.<system>.plugin`, `nixosModules.default`, `devShells.<system>.default`, and `formatter.<system>`.
Supported systems are `x86_64-linux` and `aarch64-linux`. Build each architecture natively to verify it.
