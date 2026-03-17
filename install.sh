#!/usr/bin/env bash
set -euo pipefail

REPO_SPEC="${COCO_INSTALL_SPEC:-git+https://github.com/pcopu/coco.git}"
UV_INSTALL_URL="${COCO_UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"

info() {
  printf '==> %s\n' "$*"
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi

  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

UV_BIN="$(find_uv || true)"

if [ -z "$UV_BIN" ]; then
  command -v curl >/dev/null 2>&1 || die "curl is required to bootstrap uv"
  info "uv not found; installing uv"
  curl -LsSf "$UV_INSTALL_URL" | sh
  UV_BIN="$(find_uv || true)"
fi

[ -n "$UV_BIN" ] || die "uv installation finished, but the uv binary was not found"

info "installing CoCo"
"$UV_BIN" tool install --force "$REPO_SPEC"

printf '\n'
info "CoCo is installed"
printf 'Run: coco\n'

if ! command -v coco >/dev/null 2>&1; then
  printf '\n'
  printf 'If `coco` is not on your PATH yet, restart your shell or add:\n'
  printf '  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"\n'
fi
