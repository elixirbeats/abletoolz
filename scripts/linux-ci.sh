#!/usr/bin/env bash
# Run what the release workflow runs, on Linux, before tagging.
#
# The plugin scanners are chosen by OS, so a suite that is green on Windows and
# macOS can still fail on the Linux runner. This catches that without spending a
# tag to find out.
#
# From Windows, in a WSL distro (Ubuntu 24.04 matches ubuntu-latest):
#     wsl -d Ubuntu-24.04 -- bash scripts/linux-ci.sh
#
# Anywhere Docker runs:
#     docker run --rm -v "$PWD":/src -w /src python:3.12 bash scripts/linux-ci.sh
#
# The tree is copied to a temp dir first, so a Linux venv never lands in a
# checkout that Windows is also building in.
set -euo pipefail

source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

echo "Copying $source_dir"
tar --exclude=.venv --exclude=.git --exclude=.mypy_cache --exclude=.pytest_cache \
    --exclude=.ruff_cache --exclude=dist --exclude=build \
    -C "$source_dir" -cf - . | tar -C "$work_dir" -xf -

cd "$work_dir"
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet ".[dev]"
.venv/bin/python -m pytest test -q
