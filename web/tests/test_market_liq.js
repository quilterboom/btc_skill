/**
 * 清算簇卡片 DOM 测试（jsdom）
 * 2026-11-02 · liusir 要求清算数据在前端展示
 *
 * 锁住的口径：
 *   1. 清算簇卡片存在且渲染出上方/下方两栏（上方=空单清算簇，下方=多单清算簇）
 *   2. 建议徽章：偏多/偏空/中性 + 倾向分（参考值=数值最大值口径）
 *   3. 数据不可用时如实说「暂无/不可用」，不编造
 *   4. 页面绝不直连 CoinAnk（上游 IP 限流）—— 只走 /api/liq_map
 *   5. 一个区块渲染失败不能让整页空白
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const BASE = "http://127.0.0.1:23003";
const html = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/market.html", "utf8");

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; failures.push(name + (extra ? " → " + extra : "")); console.log("  ✗ " + name + (extra ? " → " + extra : "")); }
}

// 假的 /api/liq_map 响应（可控，便于测各种分支）
const FIX = {
  available: true,
  source: "local-snapshot",
  last_price: 78148.0,
  as_of: "2026-11-02 15:10:50",
  age_sec: 120,
  stale: false,
  suggestion: { label: "偏空", score: -0.209, reference_side: "下方", reference_value: 1e9 },
  coverage_pct: [-4.35, 4.52],
  above: [
    { price: 78960, strength_pct: 79.1, dist_pct: 1.04, exchanges: 6 },
    { price: 79150, strength_pct: 65.0, dist_pct: 1.28, exchanges: 6 },
    { price: 79360, strength_pct: 43.9, dist_pct: 1.55, exchanges: 5 },
  ],
  below: [
    { price: 77500, strength_pct: 100.0, dist_pct: -0.83, exchanges: 6 },
    { price: 77300, strength_pct: 89.0, dist_pct: -1.08, exchanges: 6 },
    { price: 77100, strength_pct: 61.0, dist_pct: -1.34, exchanges: 6 },
  ],
  history: [
    { ts: 1, ts_iso: "2026-11-02 15:00:00", px: 78200, score: -0.3, label: "偏空",
      above_top: 78950, below_top: 77520, above_dist: 0.96, below_dist: -0.87 },
    { ts: 2, ts_iso: "2026-11-02 15:10:00", px: 78148, score: -0.209, label: "偏空",
      above_top: 78960, below_top: 77500, above_dist: 1.04, below_dist: -0.83 },
  ],
  cluster_drift: {
    above: { from: 0.96, to: 1.04, delta: 0.08, approaching: false },
    below: { from: -0.87, to: -0.83, delta: 0.04, approaching: true },
  },
  sample_count: 18,
};

function makeDom(payload, opts = {}) {
  return new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: BASE + "/market",
    beforeParse(window) {
      window.fetch = (url, opts2) => {
        const u = String(url);
        if (u.includes("/api/liq_map")) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve(payload),
          });
        }
        // 其它接口一律空响应，避免测试依赖真实服务
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      };
      window.__errors = [];
      window.addEventListener("error", (e) => window.__errors.push(String(e.message || e)));
    },
  });
}

function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }

(async () => {
  console.log("\n===== 清算簇卡片渲染 =====");
  {
    const dom = makeDom(FIX);
    const doc = dom.window.document;
    await wait(300);

    check("卡片区块存在（#sec-liq）", !!doc.getElementById("sec-liq"));
    check("卡片容器存在（#liq-body）", !!doc.getElementById("liq-body"));
    const body = (doc.getElementById("liq-body") || {}).innerHTML || "";
    check("渲染出「上方 · 空单清算簇」", body.includes("上方 · 空单清算簇"),
          body.slice(0, 120));
    check("渲染出「下方 · 多单清算簇」", body.includes("下方 · 多单清算簇"));
    check("含方向语义说明（上行磁吸）", body.includes("上行磁吸"));
    check("含方向语义说明（下行瀑布）", body.includes("下行瀑布"));
  }

  console.log("\n===== 建议徽章 =====");
  {
    const dom = makeDom(FIX);
    await wait(300);
    const body = dom.window.document.getElementById("liq-body").innerHTML;
    check("徽章显示偏空", body.includes("偏空"));
    check("徽章带倾向分", /倾向分\s*-?0\.\d+/.test(body), body.match(/倾向分[^<]*/)?.[0]);
    check("倾向分用参考值口径标注（页脚）", body.includes("倾向分 = (上方-下方)/参考值") ||
          body.includes("参考值 = 上下两侧强度的最大值"));
    check("提示强度是相对值", body.includes("相对值"));
  }

  console.log("\n===== 簇位置信息 =====");
  {
    const dom = makeDom(FIX);
    await wait(300);
    const body = dom.window.document.getElementById("liq-body").innerHTML;
    check("显示簇价格 78960", body.includes("78,960") || body.includes("78960"));
    check("显示强度百分比", /79%|79\.0%/.test(body));
    check("显示距现价 +1.04%", /1\.04%/.test(body));
    check("显示交易所共识 6/6", body.includes("6/6"));
    check("显示覆盖范围", body.includes("-4.35%~4.52%") || body.includes("-4.35"));
  }

  console.log("\n===== 簇位置走势 =====");
  {
    const dom = makeDom(FIX);
    await wait(300);
    const body = dom.window.document.getElementById("liq-body").innerHTML;
    check("渲染走势画布", body.includes('id="liq-spark"') || body.includes("liq-spark"));
    check("显示「正在靠近」（下方簇）", body.includes("正在靠近"));
    check("显示「正在远离」（上方簇）", body.includes("正在远离"));
  }

  console.log("\n===== 数据不可用时如实说 =====");
  {
    const dom = makeDom({ available: false, reason: "还没有清算簇快照（采集器每 10 分钟采一次，稍后刷新）" });
    await wait(300);
    const body = dom.window.document.getElementById("liq-body").innerHTML;
    check("显示不可用原因", body.includes("还没有清算簇快照"));
    check("不渲染簇表格（不编造）", !body.includes("空单清算簇"));
    check("不渲染徽章分数", !body.includes("倾向分"));
  }

  console.log("\n===== 过期快照要标注 =====");
  {
    const dom = makeDom({ ...FIX, stale: true, age_sec: 3000 });
    await wait(300);
    const body = dom.window.document.getElementById("liq-body").innerHTML;
    check("标注快照未刷新", body.includes("未刷新"));
  }

  console.log("\n===== 页面绝不直连 CoinAnk =====");
  {
    const dom = makeDom(FIX);
    const doc = dom.window.document;
    await wait(300);
    // 检查页面里没有对 coinank 的直接 fetch
    const scripts = Array.from(doc.querySelectorAll("script"))
      .map((s) => s.textContent || "").join("\n");
    check("JS 里没有直连 coinank.com", !/fetch\s*\(\s*[`"']https?:\/\/[^`"']*coinank/.test(scripts));
    check("JS 里有 /api/liq_map 调用", scripts.includes("/api/liq_map"));
  }

  console.log("\n===== 页面异常 =====");
  {
    const dom = makeDom(FIX);
    await wait(300);
    const errs = (dom.window.__errors || []).filter((e) => !/Not implemented|Could not parse CSS/.test(e));
    check("渲染过程无未捕获异常", errs.length === 0, errs.slice(0, 2).join(" | "));
  }

  console.log(`\n===== 结果：${pass} 通过 / ${fail} 失败 =====`);
  if (fail > 0) { console.log("失败项：" + failures.join("\n  ")); process.exit(1); }
})().catch((e) => { console.error(e); process.exit(1); });
