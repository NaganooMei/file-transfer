#!/usr/bin/env bash
# A5 V4 FA: Posix-only AIO, shared Cache hit, shared Posix hit.
set -euo pipefail
dry_run=0
if [[ "${1:-}" == --dry-run ]]; then dry_run=1; shift; fi
repo="${1:-/workspace/unified-cache-management}"
storage="${2:-/mnt/ucm}"
python="${PYTHON:-python}"
output="${3:-$PWD/v4-tp4-results-$(date +%Y%m%d-%H%M%S)-$$}"
data="${storage}/ucm-v4-tp4-preview"
if (( ! dry_run )); then
  test -f "${repo}/ucm/store/test/e2e/cache_bw.py"
  test -f "${repo}/ucm/store/test/e2e/posixstore_aio_test.py"
  "${python}" -c 'from ucm.store.cache.capabilities import cache_core_version; v=cache_core_version(); print("Installed Cache core:", v); assert v == 2, "This test requires a Cache v2 build"'
  mkdir -p "${storage}"
  mkdir "${output}"
  data="$(mktemp -d "${storage}/ucm-v4-tp4-XXXXXXXX")"
  mkdir "${data}/aio" "${data}/pipeline"
  # Only this invocation's newly created directory is removed.
  trap 'rm -rf -- "${data}"' EXIT
fi
run() {
  local name="$1" status=0
  shift
  printf '\nCASE %s\n' "${name}"
  printf ' %q' "$@"
  printf '\n'
  if (( dry_run )); then return; fi
  printf '\nCASE %s\n' "${name}" >> "${output}/key-output.txt"
  PYTHONUNBUFFERED=1 "$@" 2>&1 \
    | tee "${output}/${name}.log" \
    | awk '
        /^epoch=.*load=/ || /^phase=(load|warmup) / ||
        /^Load summary/ || /^rank   / || /^ALL / ||
        /^[0-9]+[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]/ ||
        /Traceback|RuntimeError:|ValueError:|ModuleNotFoundError:|\[UC\]\[E\]/ {
          print; fflush()
        }' \
    | tee -a "${output}/key-output.txt" || status=$?
  if (( status != 0 )) || grep -q '^Traceback' "${output}/${name}.log"; then
    printf 'FAILED %s; last lines of full log:\n' "${name}" | tee -a "${output}/key-output.txt"
    tail -n 20 "${output}/${name}.log" | tee -a "${output}/key-output.txt"
    return 1
  fi
}
common=(
  --cache-core v2 --model dsv4 --workers 4 --worker-mode mla
  --blocks 512 --layout whole-block --cache-capacity-gb 16
  --direct-io --cache-copy plain --cache-streams 4
  --epochs 5 --warmup 1 --epoch-interval-ms 15
)
printf 'V4 FA-only: 4 workers; 128 backend blocks/rank; 512 HBM blocks/rank; 83 fragments/block.\n'
printf 'Payload/block=3183872 bytes; aligned Posix IO/block=3186688 bytes.\n'
printf 'Posix-only repeats files: compare epoch 0 with later epochs. Pipeline Posix uses new keys each round.\n'
printf 'Temporary file data approximately 10.64 GiB; complete logs: %s\n' "${output}"
run posix_aio "${python}" "${repo}/ucm/store/test/e2e/posixstore_aio_test.py" \
  --worker-number 4 --block-number 128 --shard-size 3186688 --shard-number 1 \
  --dump-epoch-number 1 --load-epoch-number 6 \
  --posix-io-engine aio --io-direct --posix-data-trans-concurrency 32 \
  --storage-backend "${data}/aio"
run cache_hit "${python}" "${repo}/ucm/store/test/e2e/cache_bw.py" \
  --mode cache-hit "${common[@]}"
run posix_hit "${python}" "${repo}/ucm/store/test/e2e/cache_bw.py" \
  --mode posix-hit "${common[@]}" \
  --storage-backends "${data}/pipeline" --posix-engine aio --posix-concurrency 32
if (( ! dry_run )); then
  printf '\nFinished. Return this file: %s/key-output.txt\n' "${output}"
fi
