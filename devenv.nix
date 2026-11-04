{ pkgs, ... }:

let
  # The declared support matrix (pyproject.toml `requires-python`).
  matrix = {
    "3.11" = pkgs.python311;
    "3.12" = pkgs.python312;
    "3.13" = pkgs.python313;
    "3.14" = pkgs.python314;
  };
in
{
  languages.python = {
    enable = true;
    package = pkgs.python312;
    uv = {
      enable = true;
      sync.enable = true;
    };
  };

  # Optional tools the acceptance workflow drives. Salt is deliberately absent:
  # the server must install and run without it.
  packages = [
    pkgs.git
    pkgs.jq
    pkgs.neovim
    pkgs.yamlfmt
  ];

  env.UV_LINK_MODE = "copy";

  scripts = {
    check-static.exec = ''
      set -eu
      uv run --locked black --check salt_lsp tests tools
      uv run --locked mypy salt_lsp
      uv run --locked pylint --fail-under 9.2 salt_lsp/*.py
    '';

    # Full source and installed-artifact validation on every supported Python.
    # Each environment is created inside a fresh directory so `salt_lsp` can
    # only resolve to this checkout or the wheel built from it.
    check-matrix.exec = ''
      set -eu
      root=$(git rev-parse --show-toplevel)
      cd "$root"
      work=$(mktemp -d)
      trap 'rm -rf "$work"' EXIT
      uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file "$work/runtime.txt" >/dev/null
      uv build --no-sources --out-dir "$work/dist"
      ${pkgs.lib.concatStringsSep "\n" (pkgs.lib.mapAttrsToList (version: python: ''
        echo "=== Python ${version}"
        UV_PROJECT_ENVIRONMENT="$work/dev-${version}" uv sync --locked --python ${python}/bin/python
        "$work/dev-${version}/bin/python" -m pytest --basetemp="$work/pytest-${version}" -q
        uv venv --python ${python}/bin/python "$work/installed-${version}"
        uv pip install --python "$work/installed-${version}/bin/python" --constraint "$work/runtime.txt" "$work"/dist/*.whl
        "$work/installed-${version}/bin/python" tests/installed_smoke.py
        "$work/installed-${version}/bin/python" tests/installed_smoke.py "$work/installed-${version}/bin/salt_lsp_server"
      '') matrix)}
    '';
  };

  git-hooks = {
    package = pkgs.prek;
    hooks = {
      black = {
        enable = true;
        name = "black";
        entry = "uv run --locked --python ${pkgs.python312}/bin/python black --check salt_lsp tests";
        pass_filenames = false;
        types = [ "python" ];
      };
      pytest = {
        enable = true;
        name = "pytest";
        entry = "uv run --locked --python ${pkgs.python312}/bin/python pytest -q";
        pass_filenames = false;
        always_run = true;
        stages = [ "pre-commit" ];
      };
    };
  };
}
