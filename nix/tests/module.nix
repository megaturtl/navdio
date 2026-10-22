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
in
  assert pkgs.lib.assertMsg (failures == []) (builtins.concatStringsSep "\n" failures);
    pkgs.linkFarm "navdio-nixos-units" [
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
    ]
