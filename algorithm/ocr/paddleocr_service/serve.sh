#!/usr/bin/env bash
set -euo pipefail

service_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}
export OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}
export PADDLE_PDX_MODEL_SOURCE=${PADDLE_PDX_MODEL_SOURCE:-bos}

exec "${PADDLEX_BIN:-paddlex}" \
  --serve \
  --pipeline "${PADDLEOCR_PIPELINE:-$service_dir/pipeline.yaml}" \
  --device cpu \
  --host "${PADDLEOCR_HOST:-127.0.0.1}" \
  --port "${PADDLEOCR_PORT:-18888}"
