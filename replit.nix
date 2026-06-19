{pkgs}: {
  deps = [
    pkgs.glow
    pkgs.vim
    pkgs.python312Packages.mypy
    pkgs.python312Packages.pylint
    pkgs.python312Packages.pytest
    pkgs.uv
    pkgs.jq
    pkgs.nettools
    pkgs.python312
  ];
}
