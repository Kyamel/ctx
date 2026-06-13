{ pkgs ? import <nixpkgs> { } }:

# Nix provides the interpreter + uv only.
# All Python libraries are managed by uv (see pyproject.toml), NOT by nix.
pkgs.mkShell {
  packages = [
    pkgs.python312
    pkgs.uv
    pkgs.git
  ];

  shellHook = ''
    export UV_PYTHON=${pkgs.python312}/bin/python3.12
    # Keep uv-managed venv local to the project.
    export UV_PROJECT_ENVIRONMENT=".venv"
    echo "ctx dev shell — run 'uv sync' then 'uv run ctx --help'"
  '';
}
