#!/usr/bin/env bash
# Build the portable Windows server bundle: dist/fams-server-<sha>-win64.zip
#
# WHY: the recording desktop has no admin rights, so nothing may need an installer, a
# system-wide PATH change or a service. The zip carries everything the backend needs:
#   vendor/python/      relocatable CPython 3.12 (python-build-standalone, the builds uv uses)
#                       with requirements.txt preinstalled into its site-packages
#   vendor/ffmpeg/bin/  ffmpeg.exe + ffprobe.exe (gyan.dev release essentials)
#   master_backend/     the backend, with a dashboard bundle built from this same commit
#   BUILD_ID            the commit, so /health and the dashboard top bar agree
# Unzip anywhere under the user's profile, create master_backend\.env, double-click start.bat.
# start_all.ps1 sees vendor\python and skips venv/pip entirely (no network needed either).
#
# Built from the committed HEAD (git archive), never the working tree, so the zip is exactly
# one commit. Runs on Linux; needs git, curl, uv, node/npm, python3.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY_VERSION="3.12"
PBS_RELEASE="${PBS_RELEASE:-20261026}"
PBS_ASSET="cpython-3.12.16+${PBS_RELEASE}-x86_64-pc-windows-msvc-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_ASSET//+/%2B}"
FFMPEG_URL="${FFMPEG_URL:-https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/fams-dist"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "warning: uncommitted changes are NOT in the bundle (it is built from HEAD)" >&2
fi
SHA="$(git rev-parse --short HEAD)"
NAME="fams-server-${SHA}-win64"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/$NAME"
mkdir -p "$STAGE" "$CACHE" dist

echo "== source @ $SHA"
git archive --format=tar HEAD | tar -x -C "$STAGE"
# The phone app, its tests and dev tooling do not run on the server.
rm -rf "$STAGE/mobile_node" "$STAGE/.github" "$STAGE/master_backend/tests"
echo "$SHA" > "$STAGE/BUILD_ID"

echo "== dashboard bundle (BUILD_ID=$SHA)"
( cd "$STAGE/dashboard" && npm ci --no-audit --no-fund --loglevel=error && BUILD_ID="$SHA" npm run build >/dev/null )
test -f "$STAGE/master_backend/app/static/index.html"
grep -q "\"$SHA\"" "$STAGE"/master_backend/app/static/assets/index-*.js
rm -rf "$STAGE/dashboard"   # built output lives in master_backend/app/static

echo "== python $PY_VERSION (windows, relocatable)"
[ -f "$CACHE/$PBS_ASSET" ] || curl -fL --retry 3 -o "$CACHE/$PBS_ASSET" "$PBS_URL"
mkdir -p "$STAGE/vendor"
tar -xzf "$CACHE/$PBS_ASSET" -C "$STAGE/vendor"   # -> vendor/python/
test -f "$STAGE/vendor/python/python.exe"

echo "== backend dependencies (win_amd64 wheels only)"
uv pip install --quiet \
  --python-platform x86_64-pc-windows-msvc --python-version "$PY_VERSION" \
  --only-binary :all: \
  --target "$STAGE/vendor/python/Lib/site-packages" \
  -r master_backend/requirements.txt

echo "== ffmpeg"
FF_ZIP="$CACHE/ffmpeg-release-essentials.zip"
[ -f "$FF_ZIP" ] || curl -fL --retry 3 -o "$FF_ZIP" "$FFMPEG_URL"
mkdir -p "$STAGE/vendor/ffmpeg/bin"
python3 - "$FF_ZIP" "$STAGE/vendor/ffmpeg/bin" <<'EOF'
import sys, zipfile, os
src, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(src) as z:
    for n in z.namelist():
        base = os.path.basename(n)
        if n.endswith("/bin/" + base) and base in ("ffmpeg.exe", "ffprobe.exe"):
            with z.open(n) as f, open(os.path.join(out, base), "wb") as g:
                g.write(f.read())
missing = {"ffmpeg.exe", "ffprobe.exe"} - set(os.listdir(out))
sys.exit(f"ffmpeg zip lacks {missing}" if missing else 0)
EOF

echo "== zip"
OUT="$ROOT/dist/$NAME.zip"
rm -f "$OUT"
python3 - "$WORK" "$NAME" "$OUT" <<'EOF'
import sys, os, zipfile
work, name, out = sys.argv[1:]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for d, _, files in os.walk(os.path.join(work, name)):
        for f in files:
            p = os.path.join(d, f)
            z.write(p, os.path.relpath(p, work))
EOF
ls -lh "$OUT"
sha256sum "$OUT" | tee "$OUT.sha256"
