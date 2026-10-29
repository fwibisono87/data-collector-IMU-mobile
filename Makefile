# IMU Collector — task shortcuts.
#
# The rebuilt system is one Python process: master_backend/run.py serves the phones, the
# camera service and the dashboard (static build committed under
# master_backend/app/static/) on a single port. There is no Node at runtime and no port
# 3000, so there is no separate "start the frontend" step any more — `make backend`
# serves everything.
#
# On the field laptop (Windows) use start.bat / start_all.ps1 / ops\*.ps1 instead; make is
# for Linux development, and everything here runs from the repository root.

VENV_PY := master_backend/.venv/bin/python
# Prefer the venv (that is what start_all.sh creates); fall back to python3 for a quick
# `make test` on a machine without one.
PY := $(if $(wildcard $(VENV_PY)),$(VENV_PY),python3)

.PHONY: help backend dev test test-backend test-dashboard test-slow dashboard apk \
        install-phones proto proto-python proto-vectors start ip stop

help:
	@grep -E '^## ' Makefile | sed 's/^## //'

## backend: run backend + dashboard + cameras on :8000 (no reload)
backend:
	$(PY) master_backend/run.py

## dev: same, but restart on code changes (IMU_DEV_RELOAD=1; never during a recording)
dev:
	IMU_DEV_RELOAD=1 $(PY) master_backend/run.py

## test: backend fast suite + dashboard unit tests
test: test-backend test-dashboard

test-backend:
	$(PY) -m pytest master_backend/tests -q -m "not slow"

# Same probe as the launchers: without node_modules there is nothing to test, and
# `npm ci` is the only install that honours package-lock.json exactly.
dashboard/node_modules: dashboard/package-lock.json dashboard/package.json
	cd dashboard && npm ci
	@touch dashboard/node_modules

test-dashboard: dashboard/node_modules
	cd dashboard && npm test

## test-slow: camera/video/soak tests (minutes; real ffmpeg children, fake cameras)
test-slow:
	$(PY) -m pytest master_backend/tests -q -m slow

## dashboard: build the dashboard into master_backend/app/static (needs Node, commit the output)
dashboard:
	cd dashboard && npm ci && npm run build

## apk: build the phone APK -> mobile_node/app/build/outputs/apk/release/app-release.apk
apk:
	cd mobile_node && ./gradlew assembleRelease

## install-phones: install that APK on every adb device without touching their data
install-phones:
	./ops/install_phones.sh

## proto: regenerate the Python protobuf bindings and the Kotlin test vectors
proto: proto-python proto-vectors

proto-python:
	protoc --python_out=master_backend/proto -I shared_contracts shared_contracts/sensor_packet.proto shared_contracts/commands.proto

# The phone codec is hand-written, so its bytes are pinned against the backend's by a
# generated vector file rather than by another Kotlin test.
proto-vectors:
	python3 tools/gen_proto_vectors.py

## start: Windows one-click start (start_all.ps1: deps, ffmpeg check, browser, LAN IP)
start:
	powershell -NoProfile -ExecutionPolicy Bypass -File start_all.ps1

## ip: show the LAN IP, dashboard URL and /health without stopping anything
ip:
	powershell -NoProfile -ExecutionPolicy Bypass -File ops/ip.ps1

## stop: stop the backend and the ffmpeg children it spawned (never unrelated ffmpeg)
stop:
	powershell -NoProfile -ExecutionPolicy Bypass -File ops/stop.ps1