{
  pkgs,
  module,
}: let
  system = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit (pkgs.stdenv.hostPlatform) system;
    modules = [
      module
      {
        system.stateVersion = "26.05";
        boot.loader.grub.enable = false;
        fileSystems."/" = {
          device = "none";
          fsType = "tmpfs";
        };
        services.navidrome.enable = true;
        services.navidrome.settings.MusicFolder = "/srv/music";
        services.navdio = {
          enable = true;
          username = "test-broadcaster";
          libraries."1" = "/srv/music";
          tokenFile = "/run/secrets/navdio-event-token";
        };
      }
    ];
  };
  failures = map (entry: entry.message) (
    builtins.filter (entry: !entry.assertion) system.config.assertions
  );
  navidrome = system.config.services.navidrome;
  discoveryConfig = (pkgs.formats.json {}).generate "navdio-discovery.json" {
    DataFolder = "./data";
    CacheFolder = "./cache";
    MusicFolder = "./music";
    Address = "unix:./navidrome.sock";
    EnableInsightsCollector = false;
    Plugins = navidrome.settings.Plugins;
    LogLevel = "error";
  };
  units = pkgs.linkFarm "navdio-nixos-units" [
    {
      name = "navdio.socket";
      path = system.config.systemd.units."navdio.socket".unit;
    }
    {
      name = "navdio.service";
      path = system.config.systemd.units."navdio.service".unit;
    }
    {
      name = "navdio-plugin.service";
      path = system.config.systemd.units."navdio-plugin.service".unit;
    }
    {
      name = "navidrome.service";
      path = system.config.systemd.units."navidrome.service".unit;
    }
  ];
in
  assert pkgs.lib.assertMsg (failures == []) (builtins.concatStringsSep "\n" failures);
    pkgs.runCommand "navdio-nixos-module"
    {
      nativeBuildInputs = [
        pkgs.curl
        pkgs.jq
      ];
    }
    ''
      mkdir data cache music
      ${pkgs.lib.getExe navidrome.finalPackage} --nobanner --configfile ${discoveryConfig} > server.log 2>&1 &
      server=$!
      trap 'kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true' EXIT
      curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-all-errors \
        --max-time 1 --unix-socket "$PWD/navidrome.sock" http://localhost/ping > /dev/null \
        || { cat server.log; exit 1; }
      kill "$server"
      wait "$server"
      trap - EXIT
      ${pkgs.lib.getExe navidrome.finalPackage} --nobanner --configfile ${discoveryConfig} plugin rescan
      ${pkgs.lib.getExe navidrome.finalPackage} --nobanner --configfile ${discoveryConfig} plugin list --format json > plugins.json
      jq -e 'any(.[]; .id == "navdio")' plugins.json
      ln -s ${units} "$out"
    ''
