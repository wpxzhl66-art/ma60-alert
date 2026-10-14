#!/usr/bin/env bash
# 带重试的推送：远端被其他步骤/并发运行推进时先 rebase 再重推。
set -u

for i in 1 2 3 4; do
  if git push; then
    echo "[push] 成功（第 $i 次）"
    exit 0
  fi
  echo "[push] 第 $i 次失败，尝试 rebase 后重推"
  git pull --rebase --autostash origin "$(git rev-parse --abbrev-ref HEAD)" || true
  sleep 3
done

echo "[push] 连续失败，放弃"
exit 1
