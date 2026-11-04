#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/tests 包初始化 —— 测试环境全局隔离（2026-11-04）

★ 为什么必须在这里设（而不是每个测试文件各自设）：
   单元测试只要有一条路径没 mock 推送，就会把**测试夹具数据**当成真持仓推
   到生产群。实测污染 302 条（10-07 起）：`tests/test_position_tracker.py`
   构造 id="test-1000" 的假单触发 EMA 预警 → 真实推送「做多 @ 100.0 /
   已平 3500 张 / 剩余 1500 张」到生产 chat 7097652385。

   旧做法（skill 里那节）只隔离了**审计日志**路径（BTCTG_AUDIT），
   挡不住**真实发送** —— 审计干净了，群里照样被刷。

★ 三道隔离（缺一不可）：
   1. BTCTG_DRYRUN=1 → telegram.push / push_once 发送前直接 return False，
      绝不碰网络（幂等状态照常推进，测试的幂等断言仍有效）
   2. BTCTG_AUDIT=<临时文件> → 审计日志不写生产 tg_audit.log
   3. BTCTG_TEST=1 → 供业务代码识别「我在测试里」（可选，不强制）

★ 必须在 import telegram 之前设 —— telegram.py 在 import 时读这两个 env。
"""
import os
import tempfile

# 1) dry-run 闸门：绝不真发
os.environ["BTCTG_DRYRUN"] = "1"

# 2) 审计日志隔离到临时文件（不污染生产 tg_audit.log）
_AUDIT = os.path.join(tempfile.gettempdir(), "btc_test_tg_audit.log")
os.environ.setdefault("BTCTG_AUDIT", _AUDIT)

# 3) 测试标识
os.environ["BTCTG_TEST"] = "1"
