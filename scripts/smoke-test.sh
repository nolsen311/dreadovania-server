#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Smoke-test a built image with the production hardening (CI, every platform).
#   scripts/smoke-test.sh IMAGE
# Needs Docker and curl; uses ${RUNNER_TEMP:-/tmp} for the data volume.
set -euo pipefail

image="${1:?usage: scripts/smoke-test.sh IMAGE}"
data="${RUNNER_TEMP:-/tmp}/smoke-data"

install -d -m 0777 "${data}"
docker run -d --name smoke -p 8080:8080 \
  --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 256 --memory 2g \
  -v "${data}:/data" "${image}"
trap 'docker rm -f smoke >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/healthz; then break; fi
  sleep 1
done
echo
curl -fsS http://127.0.0.1:8080/api/v1/info
echo

test "$(docker exec smoke id -u)" = "10001"
if docker exec smoke touch /app/should-fail 2>/dev/null; then
  echo "::error::root filesystem is writable"
  exit 1
fi

# Randovania works inside the hardened image (ADR 0010): generate a Dread seed
# from the bundled starter preset, writing only to /data.
# shellcheck disable=SC2016 # expanded inside the container, not here
docker exec smoke sh -c 'python -m randovania layout generate-from-presets \
  --file "$(python -c "import randovania, pathlib; print(pathlib.Path(randovania.__file__).parent / \"games/dread/presets/starter_preset.rdvpreset\")")" \
  --seed-number 1 /data/smoke.rdvgame'
docker exec smoke test -s /data/smoke.rdvgame

# The compiled texture-swizzling module (built from source on arm64, ADR 0028)
# loads and round-trips data.
docker exec smoke python -c '
import os, py_tegra_swizzle as t
w, h, bpp = 64, 64, 4
bh = t.block_height_mip0(h)
src = os.urandom(w * h * bpp)
out = t.deswizzle_block_linear(w, h, 1, t.swizzle_block_linear(w, h, 1, src, bh, bpp), bh, bpp)
assert out[: len(src)] == src, "py_tegra_swizzle round trip failed"
print("py_tegra_swizzle ok")'
