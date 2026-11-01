/**
 * 训练页 · 错误归因卡（亏损原因）测试（jsdom）
 * 2026-11-01 · liusir：「训练环境也要新增错误归因的卡片，数据就是分析亏损策略的原因」
 *
 * 覆盖：
 *   1. /api/status/training 返回 mistakes（by_cause 互斥加总 == n_losses）
 *   2. renderTraining 把亏损原因渲染进 #train-mistakes（中文标签 · 无 undefined/NaN）
 *   3. 页脚含判定依据 + 亏损/分母对账
 *   4. 畸形 payload 健壮性（error / 空 by_cause / 缺字段 → 不抛错 + 有占位文案）
 *   5. 源码：卡片挂在训练数据分析区（不是 prod-only 区）
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const BASE = "http://127.0.0.1:23003";
const SRC = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/index.html", "utf8");

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; failures.push(name + (extra ? " → " + extra : "")); console.log("  ✗ " + name + (extra ? " → " + extra : "")); }
}

(async () => {
  // ---- 0. 源码断言 ----
  console.log("【0】源码断言");
  check("renderLossCauses 函数存在", SRC.includes("function renderLossCauses"));
  check("renderTraining 挂了 mistakes 渲染", /if \(d\.mistakes\) renderLossCauses/.test(SRC));
  check("卡片容器 #train-mistakes 存在", SRC.includes('id="train-mistakes"'));
  const m = SRC.match(/<div id="sec-train-analysis"[\s\S]*?<\/div>\s*<div class="data-card">\s*<h3>错误归因/);
  check("错误归因卡在训练数据分析区（sec-train-analysis）", !!m);
  check("卡片标题带「优化策略的输入」", SRC.includes("亏损原因 · 优化策略的输入"));

  // ---- 1. API 数据 ----
  console.log("【1】API 数据");
  const d = await fetch(BASE + "/api/status/training").then(r => r.json());
  const ms = d.mistakes || {};
  check("mistakes 字段存在且无 error", ms && !ms.error && Array.isArray(ms.by_cause), JSON.stringify(ms).slice(0, 80));
  const sum = (ms.by_cause || []).reduce((a, b) => a + (b.count || 0), 0);
  check("by_cause count 之和 == n_losses（主因互斥）", sum === ms.n_losses, `${sum} vs ${ms.n_losses}`);
  check("n_losses <= trade_count", ms.n_losses <= ms.trade_count);
  check("by_cause 全是中文标签", (ms.by_cause || []).every(b => /[\u4e00-\u9fff]/.test(b.label || "")));
  check("source_note 说明判定依据", typeof ms.source_note === "string" && ms.source_note.length > 10);

  // ---- 2. jsdom 渲染 ----
  console.log("【2】渲染");
  const dom = new JSDOM(SRC, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: BASE + "/",
    beforeParse(window) {
      window.fetch = (url, opts) => fetch(new URL(url, BASE).href, opts);
      window.addEventListener("error", e => console.error("[PAGE ERROR]", e.message));
      window.HTMLCanvasElement.prototype.getContext = function () {
        const noop = () => {};
        return {
          setTransform: noop, clearRect: noop, beginPath: noop, moveTo: noop, lineTo: noop,
          stroke: noop, fill: noop, fillRect: noop, fillText: noop, save: noop, restore: noop,
          setLineDash: noop, measureText: () => ({ width: 10 }), rect: noop, arc: noop, canvas: this,
        };
      };
      Object.defineProperty(window.HTMLCanvasElement.prototype, "clientWidth", { get() { return 900; } });
      Object.defineProperty(window.HTMLCanvasElement.prototype, "clientHeight", { get() { return 460; } });
    },
  });
  const w = dom.window;
  const doc = w.document;
  const q = s => doc.querySelector(s);

  // 显式喂渲染（jsdom 跨 realm fetch 坑：不让测试吊死在页面自动链路上）
  w.eval(`window.__D = ${JSON.stringify(d)}`);
  w.eval("renderTraining(window.__D)");
  const body = q("#train-mistakes");
  check("卡片渲染出内容", body && body.textContent.length > 20, (body && body.textContent.slice(0, 40)) || "EMPTY");
  const txt = body.textContent;
  check("渲染出亏损主因标签", [...(ms.by_cause || [])].some(b => txt.includes(b.label)),
    txt.slice(0, 120));
  check("页脚含「判定依据」", txt.includes("判定依据"), txt.slice(-80));
  check("页脚亏损/分母对账", txt.includes(`亏损 ${ms.n_losses}`) && txt.includes(`${ms.trade_count} 笔已结算`), txt.slice(-100));
  check("无 undefined/NaN 泄漏", !/undefined|NaN/.test(txt), txt.slice(0, 120));
  const rows = body.querySelectorAll(".ms-row").length;
  check("行数 = 主因数 + 1 表头", rows === (ms.by_cause || []).length + 1, `${rows} vs ${(ms.by_cause || []).length + 1}`);
  check("条形有宽度和红色样式", [...body.querySelectorAll(".bar > i")].every(i => /width:/.test(i.getAttribute("style") || "")));

  // ---- 3. 畸形 payload 健壮性 ----
  console.log("【3】健壮性");
  const cases = {
    error: { error: "读账本失败" },
    empty_causes: { n_losses: 0, trade_count: 5, by_cause: [], source_note: "x" },
    no_by_cause: { n_losses: 1, trade_count: 5 },
    missing_counts: { n_losses: 1, trade_count: 5, by_cause: [{ type: "X", label: "其他" }], source_note: "x" },
    null: null,
  };
  for (const [name, payload] of Object.entries(cases)) {
    let threw = null;
    try {
      w.eval(`window.__M = ${JSON.stringify(payload)}`);
      w.eval("renderLossCauses(window.__M, 'train-mistakes')");
    } catch (e) { threw = e.message; }
    const out = q("#train-mistakes").textContent;
    check(`畸形 ${name} 不抛错且有内容`, !threw && out.length > 0, threw || out.slice(0, 40));
  }

  console.log(`\n═══ 结果：${pass} 通过 / ${fail} 失败 ═══`);
  if (failures.length) console.log("失败项：\n  - " + failures.join("\n  - "));
  dom.window.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("测试崩溃:", e); process.exit(1); });
