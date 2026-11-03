#!/usr/bin/env bash
# 判读模型链凭证（2026-11-02）
#   MINIMAX_CN_API_KEY → 主模型 MiniMax-M3.1-Flash-Preview (api.minimaxi.com)
#   MIMO_API_KEY       → 备模型 mimo-v2.6-pro (token-plan-cn.xiaomimimo.com)
#
# 两者都跟 Hermes 自己的 fallback_providers 同构：主报错 → 自动降级到备。
# ★ 凭证从 /root/.hermes/.env 同步过来（Hermes 自己也在用同一套），
#   明文不进 git —— 这个文件已 chmod 600 且在 .gitignore 里。
#
# 部署：cp 到 web/.llm_keys.env，然后 systemd drop-in 加 EnvironmentFile
set -euo pipefail

SRC=/root/.hermes/.env
DST=/root/skills/btc-gate-intraday-strategy/web/.llm_keys.env

mm=""
mimo=""
if [[ -f "$SRC" ]]; then
  mm=$(grep -E '^MINIMAX_CN_API_KEY=' "$SRC" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r\n' || true)
fi
old=/root/skills/btc-gate-intraday-strategy/web/.mimo_key.env
if [[ -f "$old" ]]; then
  mimo=$(grep -E '^MIMO_API_KEY=' "$old" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r\n' || true)
fi

umask 077
{
  [[ -n "$mm"   ]] && echo "MINIMAX_CN_API_KEY=$mm"
  [[ -n "$mimo" ]] && echo "MIMO_API_KEY=$mimo"
} > "$DST"

chmod 600 "$DST"
echo "已写入 $DST"
echo "  MINIMAX_CN_API_KEY: $([[ -n "$mm" ]] && echo 有 || echo 缺)"
echo "  MIMO_API_KEY:       $([[ -n "$mimo" ]] && echo 有 || echo 缺)"
