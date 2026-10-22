{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.navdio;
  navidrome = config.services.navidrome;
  json = pkgs.formats.json { };
  libraryRoots = builtins.attrValues cfg.libraries;
  runtimeDirectory = "/run/navdio";
  companionConfig = json.generate "navdio.json" {
    inherit (cfg)
      listenAddress
      port
      sourcePort
      libraries
      bitrateKbps
      replaygain
      maxListeners
      ;
    inherit runtimeDirectory;
    tokenFile = "/run/credentials/navdio.service/event-token";
    mpd = lib.getExe pkgs.mpd;
  };
  pluginConfig = json.generate "navdio-plugin.json" {
    inherit (cfg) username player;
    companionUrl = "http://127.0.0.1:${toString cfg.port}";
  };
  serverConfig = json.generate "navdio-navidrome.json" navidrome.settings;
  dataDirectory = navidrome.settings.DataFolder or "/var/lib/navidrome";
  writableNavidromePaths = [
    dataDirectory
  ]
  ++ lib.optional (navidrome.settings ? CacheFolder) navidrome.settings.CacheFolder
  ++ lib.optional (navidrome.settings ? Backup.Path) navidrome.settings.Backup.Path;
in
{
  options.services.navdio = {
    enable = lib.mkEnableOption "Share your Navidrome listening session with the world";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../packages/companion.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ../packages/companion.nix { }";
      description = "The Navdio companion package to use.";
    };
    pluginPackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../packages/plugin.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ../packages/plugin.nix { }";
      description = ''
        The Navdio plugin package to install through
        {option}`services.navidrome.plugins`.
      '';
    };
    username = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "radio";
      description = ''
        Existing Navidrome user whose playback will be broadcast.
        Select this user explicitly. Other users do not receive plugin access.
      '';
    };
    player = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "Symfonium";
      description = ''
        Case-insensitive client-name filter for the selected user.
        An empty string permits all of that user's compatible clients.
      '';
    };
    libraries = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        "1" = "/srv/music";
      };
      description = ''
        Allowed Navidrome library IDs mapped to absolute, read-only native
        music roots. The IDs must match existing Navidrome libraries.
      '';
    };
    tokenFile = lib.mkOption {
      type = lib.types.str;
      example = "/run/secrets/navdio-event-token";
      description = ''
        Absolute runtime path to the shared event token for systemd credentials.
        Create this file with a secret manager like sops.
      '';
    };
    configurePlugin = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether to configure and enable the plugin before Navidrome starts.
        It grants read access only to the selected user and libraries.
        If disabled, the administrator must configure the plugin and grants.
      '';
    };
    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Companion HTTP listen address.
        A wildcard address also accepts the local plugin connection.
        Public exposure requires a reverse proxy to hide private endpoints.
      '';
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 8750;
      description = "Companion HTTP port.";
    };
    sourcePort = lib.mkOption {
      type = lib.types.port;
      default = 8751;
      description = "Internal, loopback-only MPD HTTP port. Do not expose publicly.";
    };
    bitrateKbps = lib.mkOption {
      type = lib.types.ints.between 6 510;
      default = 32;
      description = "Opus constrained variable bitrate target in kbps.";
    };
    replaygain = lib.mkOption {
      type = lib.types.enum [
        "track"
        "album"
        "off"
      ];
      default = "track";
      description = "MPD ReplayGain mode, with peak limiting.";
    };
    maxListeners = lib.mkOption {
      type = lib.types.ints.between 1 1024;
      default = 40;
      description = "Maximum concurrent stream connections, including connections through a reverse proxy.";
    };
    supplementaryGroups = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "music" ];
      description = ''
        Existing groups that grant the companion read and traverse access to
        music.
      '';
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = navidrome.enable;
        message = "Navdio requires a local services.navidrome instance.";
      }
      {
        assertion = lib.versionAtLeast navidrome.package.version "0.64.2";
        message = "Navdio requires Navidrome >= 0.64.2.";
      }
      {
        assertion = cfg.username != "";
        message = "services.navdio.username must explicitly select a broadcast user.";
      }
      {
        assertion =
          cfg.libraries != { }
          && lib.all (key: builtins.match "[1-9][0-9]*" key != null) (builtins.attrNames cfg.libraries);
        message = "services.navdio.libraries must map positive Navidrome library IDs to music roots.";
      }
      {
        assertion = lib.all (root: lib.hasPrefix "/" root && root != "/") libraryRoots;
        message = "Navdio library roots must be absolute paths other than /.";
      }
      {
        assertion = lib.hasPrefix "/" cfg.tokenFile && !lib.hasPrefix builtins.storeDir cfg.tokenFile;
        message = "services.navdio.tokenFile must be an absolute runtime secret path outside the Nix store.";
      }
      {
        assertion = cfg.port != cfg.sourcePort;
        message = "Navdio HTTP and internal MPD ports must be different.";
      }
      {
        assertion =
          !cfg.configurePlugin
          || builtins.elem cfg.listenAddress [
            "127.0.0.1"
            "0.0.0.0"
          ];
        message = "Automatic plugin setup connects to 127.0.0.1. listenAddress must accept IPv4 loopback.";
      }
    ];
    services.navidrome.plugins = [ cfg.pluginPackage ];
    services.navidrome.settings.Plugins.Enabled = true;
    users.users.navdio = {
      isSystemUser = true;
      group = "navdio";
    };
    users.groups.navdio = { };
    systemd.services.navdio = {
      description = "Share your Navidrome listening session with the world";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" ];
      unitConfig.RequiresMountsFor = libraryRoots;
      restartTriggers = [ companionConfig ];
      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} --config ${companionConfig}";
        User = "navdio";
        Group = "navdio";
        SupplementaryGroups = cfg.supplementaryGroups;
        LoadCredential = [ "event-token:${cfg.tokenFile}" ];
        RuntimeDirectory = "navdio";
        RuntimeDirectoryMode = "0700";
        Restart = "on-failure";
        RestartSec = 5;
        TimeoutStopSec = 15;
        KillMode = "control-group";
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        ReadOnlyPaths = libraryRoots;
      };
    };
    systemd.services.navdio-plugin = lib.mkIf cfg.configurePlugin {
      description = "Configure Navdio's Navidrome plugin";
      wantedBy = [ "navidrome.service" ];
      before = [ "navidrome.service" ];
      after = [
        "navdio.service"
        "systemd-tmpfiles-setup.service"
      ];
      requires = [ "navdio.service" ];
      unitConfig.RequiresMountsFor = writableNavidromePaths;
      environment.ND_LOGLEVEL = "error";
      serviceConfig = {
        Type = "oneshot";
        User = navidrome.user;
        Group = navidrome.group;
        WorkingDirectory = dataDirectory;
        EnvironmentFile = lib.mkIf (navidrome.environmentFile != null) [
          navidrome.environmentFile
        ];
        LoadCredential = [ "event-token:${cfg.tokenFile}" ];
        ExecStart = lib.escapeShellArgs [
          "${pkgs.python3}/bin/python"
          "${../../companion/scripts/configure-plugin.py}"
          (lib.getExe navidrome.finalPackage)
          "${serverConfig}"
          "${pluginConfig}"
          cfg.username
          (builtins.toJSON (map lib.toInt (builtins.attrNames cfg.libraries)))
        ];
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = writableNavidromePaths;
      };
    };
    systemd.services.navidrome.restartTriggers = lib.optionals cfg.configurePlugin [
      pluginConfig
      cfg.pluginPackage
    ];
  };
}
