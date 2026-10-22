{
  lib,
  fetchurl,
  python3Packages,
}:
if lib.versionOlder python3Packages.pillow.version "13.0.0"
then
  python3Packages.pillow.overrideAttrs (old: {
    prePatch =
      (old.prePatch or "")
      + ''
        # Match the upstream patch's equivalent tuple syntax.
        substituteInPlace src/PIL/XbmImagePlugin.py \
          --replace-fail '(0, 0) + self.size' '(0, 0, *self.size)'
      '';
    patches =
      (old.patches or [])
      ++ [
        (fetchurl {
          name = "pillow-CVE-2026-112399.patch";
          url = "https://github.com/python-pillow/Pillow/commit/b564ea660bb3d56fcadaa7f29b4fc93a8f9e5c91.patch";
          hash = "sha256-DwTxWp4L+/JWUcZq81FqGSfD9ZBI3PO0LFXasIXh7Ew=";
        })
        (fetchurl {
          name = "pillow-CVE-2026-112901.patch";
          url = "https://github.com/python-pillow/Pillow/commit/2852a3e3d568997ddec745fc800991f138878f4b.patch";
          hash = "sha256-mXMcXwmzddmskFIWRMhT8btd39Kj2ENy5QGHMrzTW6c=";
        })
      ];
  })
else python3Packages.pillow
