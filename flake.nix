{
  description = "Share your Navidrome listening session with the world";
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  outputs = {
    self,
    nixpkgs,
    treefmt-nix,
  }: let
    systems = [
      "x86_64-linux"
      "aarch64-linux"
    ];
    forEachSystem = nixpkgs.lib.genAttrs systems;
    pkgsFor = system: import nixpkgs {inherit system;};
    treefmtFor = system: treefmt-nix.lib.evalModule (pkgsFor system) ./treefmt.nix;
  in {
    nixosModules.default = import ./nix/modules/navdio.nix;
    packages = forEachSystem (
      system: let
        pkgs = pkgsFor system;
      in {
        default = self.packages.${system}.navdio;
        navdio = pkgs.callPackage ./nix/packages/companion.nix {};
        plugin = pkgs.callPackage ./nix/packages/plugin.nix {};
      }
    );
    formatter = forEachSystem (system: (treefmtFor system).config.build.wrapper);
    devShells = forEachSystem (
      system: let
        pkgs = pkgsFor system;
        pillow = pkgs.callPackage ./nix/packages/pillow.nix {};
      in {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [
              pillow
              ps.python-mpd2
              ps.setuptools
            ]))
            pkgs.go
            pkgs.gopls
            pkgs.mpd
            pkgs.navidrome
            pkgs.ffmpeg-headless
            pkgs.ruff
            pkgs.ty
            pkgs.zip
            (treefmtFor system).config.build.wrapper
          ];
          PYTHONDONTWRITEBYTECODE = "1";
          PYTHONNOUSERSITE = "1";
          shellHook = ''
            export PYTHONPATH="$PWD/companion/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      }
    );
    checks = forEachSystem (
      system: let
        pkgs = pkgsFor system;
        pillow = pkgs.callPackage ./nix/packages/pillow.nix {};
        python = pkgs.python3.withPackages (ps: [
          pillow
          ps.python-mpd2
        ]);
      in {
        formatting = (treefmtFor system).config.build.check self;
        companion = self.packages.${system}.navdio;
        plugin = self.packages.${system}.plugin;
        python-tests =
          pkgs.runCommand "navdio-python-tests"
          {
            nativeBuildInputs = [
              python
              pkgs.mpd
            ];
          }
          ''
            export PYTHONPATH=${self}/companion/src PYTHONDONTWRITEBYTECODE=1
            python -B -m unittest discover -s ${self}/companion/tests -v
            touch "$out"
          '';
        go-tests = self.packages.${system}.plugin.overrideAttrs (_: {
          pname = "navdio-go-tests";
          buildPhase = ''
            go test ./...
            go vet ./...
          '';
          installPhase = "touch $out";
        });
        integration =
          pkgs.runCommand "navdio-integration"
          {
            nativeBuildInputs = [
              python
              pkgs.mpd
              pkgs.navidrome
              pkgs.ffmpeg-headless
            ];
            NAVDIO_COMPANION_EXE = "${self.packages.${system}.navdio}/bin/navdio";
            NAVDIO_PLUGIN_ARCHIVE = "${self.packages.${system}.plugin}/share/navdio/navdio.ndp";
          }
          ''
            export PYTHONPATH=${self}/companion/src PYTHONDONTWRITEBYTECODE=1
            python -B -m unittest discover -s ${self}/companion/tests -p test_integration.py -v
            touch "$out"
          '';
        module = import ./nix/tests/module.nix {
          inherit pkgs;
          module = self.nixosModules.default;
        };
        typing =
          pkgs.runCommand "navdio-typing"
          {
            nativeBuildInputs = [
              pkgs.ty
              python
            ];
          }
          ''
            ty check --project ${self} --python ${python}/bin/python --error-on-warning
            touch "$out"
          '';
      }
    );
  };
}
