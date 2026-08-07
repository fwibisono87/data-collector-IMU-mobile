#!/usr/bin/env bash
# Remux raw MediaRecorder WebM into a seekable file with a real duration.
#
# MediaRecorder emits a *live* bytestream: unset Segment size, no Duration in
# Info, no Cues. Concatenated chunks play but cannot be scrubbed and report no
# end time. `-c copy` rewrites the container and builds the index while passing
# the compressed frames through untouched — lossless, no re-encode.
#
# Usage:  tools/fix_webm.sh FILE...            (writes <name>_fixed.webm next to each)
#         tools/fix_webm.sh *_RESCUED.webm
set -uo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $0 FILE..." >&2
  exit 2
fi

command -v ffmpeg  >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found" >&2; exit 1; }

dur() {  # duration in seconds, or empty if the container doesn't declare one
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | head -1
}
frames() {
  ffprobe -v error -count_packets -select_streams v:0 \
          -show_entries stream=nb_read_packets -of csv=p=0 "$1" 2>/dev/null | head -1
}

fail=0
for in in "$@"; do
  [ -f "$in" ] || { echo "skip (not a file): $in" >&2; fail=1; continue; }
  case "$in" in *_fixed.webm) echo "skip (already fixed): $in"; continue;; esac

  out="${in%.*}_fixed.webm"
  before=$(dur "$in")
  echo "=== $in"
  echo "    before: duration=${before:-<none>}  size=$(stat -c%s "$in")"

  # Plain stream copy first. Only if the result still has no usable duration do
  # we retry with generated timestamps, which can shift framing slightly.
  if ! ffmpeg -nostdin -v error -y -i "$in" -c copy -map 0:v:0 "$out" 2>/tmp/_fw.err; then
    echo "    ffmpeg failed:"; sed 's/^/      /' /tmp/_fw.err; fail=1; continue
  fi

  after=$(dur "$out")
  if [ -z "$after" ] || [ "${after%%.*}" -le 0 ] 2>/dev/null; then
    echo "    no duration after copy — retrying with +genpts"
    ffmpeg -nostdin -v error -y -fflags +genpts -i "$in" -c copy -map 0:v:0 "$out" 2>/tmp/_fw.err \
      || { echo "    genpts retry failed:"; sed 's/^/      /' /tmp/_fw.err; fail=1; continue; }
    after=$(dur "$out")
  fi

  nf=$(frames "$out")
  echo "    after:  duration=${after:-<none>}  frames=${nf:-?}  size=$(stat -c%s "$out")"
  if [ -n "$after" ] && [ -n "$nf" ] && [ "${after%%.*}" -gt 0 ]; then
    echo "    avg fps: $(awk -v f="$nf" -v d="$after" 'BEGIN{printf "%.2f", f/d}')"
  fi
done

rm -f /tmp/_fw.err
exit $fail
