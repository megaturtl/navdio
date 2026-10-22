{...}: {
  projectRootFile = "flake.nix";
  programs.alejandra.enable = true;
  programs.gofmt.enable = true;
  programs.ruff-check = {
    enable = true;
    priority = 1;
  };
  programs.ruff-format = {
    enable = true;
    priority = 2;
  };
  settings.excludes = [
    "flake.lock"
    "plugin/go.sum"
    ".direnv/**"
    "**/__pycache__/**"
    "**/*.egg-info/**"
    "**/.ruff_cache/**"
    "**/.venv/**"
    "**/build/**"
    "**/dist/**"
    "result*"
  ];
}
