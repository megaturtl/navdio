{
  lib,
  buildGoModule,
  zip,
}:
buildGoModule {
  pname = "navdio-plugin";
  version = "0.1.0";
  src = lib.cleanSource ../../plugin;
  vendorHash = "sha256-VCpJsURTCrtkRP6BDFrhQljfJ1/5TrT6H1uWhfW9OY8=";
  nativeBuildInputs = [zip];
  doCheck = false;
  buildPhase = ''
    runHook preBuild
    GOOS=wasip1 GOARCH=wasm go build -trimpath -buildmode=c-shared -o plugin.wasm .
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p bundle "$out/share/navdio"
    cp plugin.wasm manifest.json bundle/
    touch -t 198001010000 bundle/*
    (cd bundle && zip -X "$out/share/navdio/navdio.ndp" manifest.json plugin.wasm)
    runHook postInstall
  '';
  passthru = {
    isNavidromePlugin = true;
    bundleName = "navdio";
  };
  meta = {
    description = "Navidrome playback-event plugin for Navdio";
    platforms = lib.platforms.linux;
  };
}
