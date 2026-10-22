{
  lib,
  python3Packages,
  mpd,
}:
python3Packages.buildPythonApplication {
  pname = "navdio";
  version = "0.1.0";
  src = lib.fileset.toSource {
    root = ../../companion;
    fileset = lib.fileset.unions [
      ../../companion/src
      ../../companion/pyproject.toml
    ];
  };
  pyproject = true;
  build-system = [python3Packages.setuptools];
  dependencies = with python3Packages; [
    pillow
    python-mpd2
  ];
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [mpd])
  ];
  pythonImportsCheck = ["navdio"];
  meta = {
    description = "Share your Navidrome listening session with the world";
    mainProgram = "navdio";
    platforms = lib.platforms.linux;
  };
}
