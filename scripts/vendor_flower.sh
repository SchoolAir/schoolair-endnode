#!/usr/bin/env bash
# scripts/vendor_flower.sh — bundle the wilting flower into this firmware.
#
# The flower's code lives in SchoolAir/Flower-End-node, which is private, and a
# unit has no GitHub credentials, so units can't fetch it. Instead, at release
# time, this script copies the few files a unit needs, at one pinned commit,
# into flower/ here. schoolair_setup.sh installs them from there, offline, on
# every setup and OTA update.
#
# Run on a machine that can read Flower-End-node (your own GitHub login):
#     scripts/vendor_flower.sh <commit>
# then run the tests, commit flower/, and release as usual. Test that commit
# on a bench unit first.
#
# flower/SOURCE records the commit and a sha256 per file;
# tests/test_flower_install.py fails if a vendored file no longer matches it,
# so a hand edit here can't drift silently away from the pinned commit.
set -euo pipefail

REF="${1:?usage: scripts/vendor_flower.sh <Flower-End-node commit>}"
REPO_URL="${FLOWER_REPO_URL:-https://github.com/SchoolAir/Flower-End-node.git}"
# What a unit needs. Not install.sh, tests/, README or bench notes.
FILES=(flower_service.py step.py deploy/schoolair-flower.service calibration.example.json)

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${HERE}/flower"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git -C "$TMP" init -q
git -C "$TMP" fetch -q --depth 1 "$REPO_URL" "$REF"
SHA="$(git -C "$TMP" rev-parse FETCH_HEAD)"

rm -rf "$DEST"
mkdir -p "$DEST"
{
    echo "# Vendored by scripts/vendor_flower.sh — do not edit these files here;"
    echo "# change them in SchoolAir/Flower-End-node and re-run the script."
    echo "repo ${REPO_URL}"
    echo "commit ${SHA}"
} > "${DEST}/SOURCE"
for f in "${FILES[@]}"; do
    git -C "$TMP" show "FETCH_HEAD:${f}" > "${DEST}/$(basename "$f")"
    echo "sha256 $(shasum -a 256 "${DEST}/$(basename "$f")" | cut -d' ' -f1) $(basename "$f")" >> "${DEST}/SOURCE"
done
chmod 755 "${DEST}/flower_service.py" "${DEST}/step.py"

echo "Vendored Flower-End-node ${SHA:0:7} into flower/:"
ls -1 "$DEST"
