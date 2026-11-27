#!/usr/bin/env bash
# Every check that guards main and the server deploy, run on a clean clone of HEAD so the
# result matches CI: no local .env, no untracked files, no uncommitted edits.
#   1. CI "test":    uv sync --locked, then uv run pytest
#   2. pre-commit:   the hooks from .pre-commit-config.yaml on every tracked file
#   3. CI "secrets": gitleaks over the whole git history (CI scans the pushed commits)
#   4. --docker:     build the server image, import every module in it and start Chromium
# Usage: scripts/check.sh [--docker] [--platform linux/arm64]
set -euo pipefail
cd "$(dirname "$0")/.."

docker=0
platform=""
while [ $# -gt 0 ]; do
  case "$1" in
    --docker) docker=1 ;;
    --platform) platform="$2"; shift ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n== %s\n' "$*"; }

if [ -n "$(git status --porcelain)" ]; then
  echo "note: uncommitted or untracked changes are not checked; CI only sees commits" >&2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git clone --quiet --no-local . "$work/repo"
git -C "$work/repo" checkout --quiet --detach "$(git rev-parse HEAD)"
echo "checking $(git rev-parse --short HEAD) in a clean clone"

step "CI test: uv sync --locked"
(cd "$work/repo" && uv sync --locked --quiet)
step "CI test: uv run pytest"
(cd "$work/repo" && uv run pytest -q -p no:cacheprovider)

step "pre-commit hooks"
(cd "$work/repo" && uvx pre-commit run --all-files)

step "CI secrets: gitleaks over the whole history"
gitleaks=$(command -v gitleaks || find ~/.cache/pre-commit -path '*/bin/gitleaks' -type f \
  -perm -u+x 2>/dev/null | head -n 1)
if [ -z "$gitleaks" ]; then
  echo "gitleaks not found: install it, or run uvx pre-commit once to build it" >&2
  exit 1
fi
"$gitleaks" git --redact --no-banner .

if [ "$docker" = 1 ]; then
  tag=linkedin-bot:check
  step "Docker: build $tag ${platform:+($platform)}"
  docker build ${platform:+--platform "$platform"} --build-arg APP_UID="$(id -u)" \
    -f "$work/repo/deploy/Dockerfile" -t "$tag" "$work/repo"
  step "Docker: import every module and start Chromium"
  docker run --rm ${platform:+--platform "$platform"} --entrypoint python3 "$tag" -c '
import importlib, pathlib
for path in sorted(pathlib.Path("/app/bot").glob("*.py")):
    importlib.import_module("bot." + path.stem)
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])  # Default seccomp: no user namespaces.
    print("modules import, Chromium", browser.version, "starts")
    browser.close()
'
  docker image rm --force "$tag" >/dev/null
fi

step "all checks passed"
