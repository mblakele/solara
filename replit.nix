{pkgs}: {
  deps = [
    pkgs.python312Packages.pytest_7
    pkgs.uv
    pkgs.jq
    pkgs.nettools
    pkgs.python312
  ];
}
