# start.ps1 - daily start on a machine that is already set up (Windows).
#
# Identical to start_all.ps1 except it never creates or repairs the venv and never runs
# pip: by day two those are done, and `pip install -r requirements.txt` before every
# recording is pure risk (a flaky network in the lab turns a working laptop into a
# broken one minutes before a subject arrives).
#
# Run start_all.ps1 (or start.bat) on a fresh laptop, after a `git pull` that changed
# requirements.txt, or whenever the backend refuses to start with an ImportError.
#
# Everything else - .env check, stopping the old backend and its ffmpeg children, the
# ffmpeg/ffprobe check, the /health wait, the dashboard URL, the phone instructions -
# lives in start_all.ps1 so there is exactly one implementation of the field procedure.
$launcher = Join-Path $PSScriptRoot 'start_all.ps1'
& $launcher -NoInstall