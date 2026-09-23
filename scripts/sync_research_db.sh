#!/usr/bin/env bash
# DSN はシェルで展開せず、Python のメモリ内で取得・受け渡しする。
set +x
set -eu

cd "$(dirname "$0")/.."
python_bin="${RESEARCH_PYTHON:-.venv/bin/python}"
worker_pid=""
signal_status=0

cleanup() {
  trap '' INT TERM
  if [[ -n "$worker_pid" ]]; then
    kill -TERM "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'signal_status=130' INT
trap 'signal_status=143' TERM

"$python_bin" -m trading.storage.research_mirror_tunnel "$@" &
worker_pid=$!
if [[ "$signal_status" -ne 0 ]]; then
  exit "$signal_status"
fi
status=0
wait "$worker_pid" || status=$?
if [[ "$signal_status" -ne 0 ]]; then
  exit "$signal_status"
fi
worker_pid=""
exit "$status"
