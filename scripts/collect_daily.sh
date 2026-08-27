#!/usr/bin/env bash
# 日次データ収集（cron 用）。
#
# crontab 例（JST、平日朝: 前営業日の H.15 反映後 / 夜: 08:30 ET 統計の当日反映）:
#   30 7 * * 2-6 /Users/yusuke/Products/fx-trading-platform/scripts/collect_daily.sh >> /Users/yusuke/Products/fx-trading-platform/logs/collect.log 2>&1
#   0 22 * * 1-5 /Users/yusuke/Products/fx-trading-platform/scripts/collect_daily.sh >> /Users/yusuke/Products/fx-trading-platform/logs/collect.log 2>&1
#
# - 冪等: 各 collector は新しい vintage / event のみ保存する（再実行・重複起動とも安全）
# - 1 ソースの失敗で残りを止めない。失敗は exit code とログに残す
set -u

cd "$(dirname "$0")/.."
set -a
source .env
set +a

PY=.venv/bin/python
ENV_NAME="${TRADING_ENV:-demo}"
failed=0

echo "=== collect_daily $(date -u '+%Y-%m-%dT%H:%M:%SZ') env=$ENV_NAME ==="

for source_name in alfred bls bea census boe boe_ois ons ecb eurostat; do
  if ! "$PY" -m trading.data.macro.collector --env "$ENV_NAME" --source "$source_name"; then
    echo "FAILED: $source_name"
    failed=1
  fi
done

if ! "$PY" -m trading.data.policy.collector --env "$ENV_NAME"; then
  echo "FAILED: policy"
  failed=1
fi

if ! "$PY" -m trading.data.intervention.collector --env "$ENV_NAME"; then
  echo "FAILED: intervention"
  failed=1
fi

exit "$failed"
