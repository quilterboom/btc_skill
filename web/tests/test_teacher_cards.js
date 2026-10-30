/**
 * 策略工作台 DOM 功能测试（jsdom）
 * 2026-10-28 · 验证四项改动：
 *   1. 老师单两张分析卡渲染（数据来自 /api/teacher/analysis → IMA KB）
 *   2. 交易明细改悬浮列表框（卡片点击唤起）
 *   3. 刷新按钮不再旋转（静态禁用态）
 *   4. 行情剧震事件触发（checkShockTrigger 定时轮询）
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const BASE = "http://127.0.0.1:23003";
const html = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/index.html", "utf8");

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; failures.push(name + (extra ? " → " + extra : "")); console.log("  ✗ " + name + (extra ? " → " + extra : "")); }
}

(async () => {
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: BASE + "/",
    beforeParse(window) {
      window.fetch = (url, opts) => fetch(new URL(url, BASE).href, opts);
      window.addEventListener("error", e => {
        console.error("[PAGE ERROR]", e.message, e.error && e.error.stack);
      });
    },
  });

  // 等所有 async 渲染（老师单分析 / 结算 / 策略）落盘
  const q = sel => dom.window.document.querySelector(sel);
  for (let i = 0; i < 40; i++) {
    const t = q("#teacher-pnl");
    const e = q("#teacher-entry");
    if (t && t.innerHTML.length > 100 && e && e.innerHTML.length > 100) break;
    await new Promise(r => setTimeout(r, 150));
  }

  console.log("\n── ① 老师单分析卡 ──");
  const tp = q("#teacher-pnl");
  const te = q("#teacher-entry");
  check("#teacher-pnl 有内容", tp && tp.innerHTML.length > 100,
    tp ? `len=${tp.innerHTML.length}` : "未找到");
  check("#teacher-entry 有内容", te && te.innerHTML.length > 100,
    te ? `len=${te.innerHTML.length}` : "未找到");
  // ★ 2026-10-30：不写死数字（老师单会变，10-30 结算 #16 后 310.29→329.13），
  //   改成跟 /api/teacher/analysis 实际返回值比对 —— 数字变了断言自动跟着变，
  //   真正出错（渲染没跟上 API）才会失败。
  const taApi = await fetch(BASE + "/api/teacher/analysis").then(r => r.json());
  const taSum = taApi.summary || {};
  const taNetStr = (taSum.net_usd >= 0 ? "+" : "") + taSum.net_usd.toFixed(2);
  check("#teacher-pnl 显示累计净利（与 API 一致）",
    tp.innerHTML.includes(taNetStr), `期望含 ${taNetStr}`);
  check("#teacher-pnl 显示胜率（与 API 一致）",
    tp.innerHTML.includes(String(taSum.win_rate_pct)),
    `期望含 ${taSum.win_rate_pct}`);
  check("#teacher-pnl 出场模式分布存在", /TP1/.test(tp.innerHTML));
  check("#teacher-pnl 每笔净利柱存在", (tp.querySelectorAll(".pnl-bar-day").length >= 10),
    `bars=${tp.querySelectorAll(".pnl-bar-day").length}`);
  check("#teacher-entry 显示入场位置（与 API 一致）",
    te.innerHTML.includes(String((taApi.profile || {}).range_pos_pct)),
    `期望含 ${(taApi.profile || {}).range_pos_pct}`);
  check("#teacher-entry 显示顺势胜率", /100/.test(te.innerHTML));
  check("#teacher-entry 逐笔明细表有行", te.querySelectorAll("tbody tr").length >= 10,
    `rows=${te.querySelectorAll("tbody tr").length}`);
  check("数据源标签显示 IMA", /IMA/.test(q("#teacher-pnl-source").textContent),
    q("#teacher-pnl-source").textContent);

  console.log("\n── ② 交易明细悬浮列表框 ──");
  const modal = q("#dt-modal");
  check("#dt-modal 容器存在", !!modal);
  check("#dt-modal 默认隐藏", !modal.classList.contains("open"));
  check("老师单卡 data-dt-open=teacher", q("#card-teacher-pnl").dataset.dtOpen === "teacher");
  check("入场卡 data-dt-open=teacher", q("#card-teacher-entry").dataset.dtOpen === "teacher");
  check("Edge Score 卡 data-dt-open=settle", q("#card-edge").dataset.dtOpen === "settle");
  check("三张卡都有 dt-clickable 可点样式",
    q("#card-teacher-pnl").classList.contains("dt-clickable") &&
    q("#card-teacher-entry").classList.contains("dt-clickable") &&
    q("#card-edge").classList.contains("dt-clickable"));
  check("页面流里不再有常驻 sec-trades 区块", !q("#sec-trades"));

  // 点击老师单卡 → 弹窗打开
  q("#card-teacher-pnl").click();
  check("点击老师单卡 → 弹窗打开", modal.classList.contains("open"));
  check("弹窗标题 = 老师单交易明细", q("#dt-modal-title").textContent === "老师单交易明细",
    q("#dt-modal-title").textContent);
  check("弹窗内含老师直播单面板", !!q("#dt-modal-bd #panel-teacher"));
  check("弹窗内含策略结算面板", !!q("#dt-modal-bd #panel-settle"));
  // ★ 2026-10-30 数据源隔离：老师单卡 → 只显示老师面板
  check("老师单卡 → data-view=teacher", modal.dataset.view === "teacher", modal.dataset.view);

  // 点遮罩关闭
  modal.click();
  check("点遮罩 → 弹窗关闭", !modal.classList.contains("open"));

  // Esc 关闭
  q("#card-edge").click();
  check("点 Edge Score 卡 → 弹窗打开", modal.classList.contains("open"));
  check("弹窗标题 = 平台策略单交易明细", q("#dt-modal-title").textContent === "平台策略单交易明细",
    q("#dt-modal-title").textContent);
  // ★ 2026-10-30 数据源隔离：系统策略卡 → 只显示策略面板
  check("Edge Score 卡 → data-view=settle", modal.dataset.view === "settle", modal.dataset.view);
  dom.window.document.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  check("Esc → 弹窗关闭", !modal.classList.contains("open"));

  // 侧栏 nav 路径
  q("#nav-trades").click();
  check("侧栏「交易明细」→ 弹窗打开", modal.classList.contains("open"));
  check("弹窗标题 = 交易明细", q("#dt-modal-title").textContent === "交易明细");
  check("侧栏 → data-view=both", modal.dataset.view === "both", modal.dataset.view);

  console.log("\n── ②b 数据源隔离 CSS 规则 ──");
  check("CSS 有 teacher→隐藏策略面板规则",
    /\.dt-modal\[data-view="teacher"\]\s*#panel-settle\s*\{\s*display:\s*none/.test(html));
  check("CSS 有 settle→隐藏老师面板规则",
    /\.dt-modal\[data-view="settle"\]\s*#panel-teacher\s*\{\s*display:\s*none/.test(html));
  check("CSS 单面板时网格退化 1 列",
    /\[data-view="teacher"\]\s*#sec-trades-grid[\s\S]{0,120}grid-template-columns:\s*1fr/.test(html));
  check("平台策略单面板汇总行 5 个指标齐全",
    !!q("#settle-stat-total") && !!q("#settle-stat-settled") && !!q("#settle-stat-pending")
    && !!q("#settle-stat-net") && !!q("#settle-stat-winrate"));
  check("平台策略单面板标题 = 平台策略单",
    q("#settle-title").textContent === "平台策略单", q("#settle-title").textContent);
  check("存在数据存疑角标元素", !!q("#settle-suspect-note"));
  check("源码含 suspect 角标渲染", /tc-suspect/.test(html));
  check("openDtModal 已改签名（第一参是 view）",
    /function openDtModal\(view, title, sub\)/.test(html));

  console.log("\n── ③ 刷新按钮不旋转 ──");
  const css = html;
  check("已删除 ma-spin 旋转动画", !/@keyframes\s+ma-spin/.test(css));
  check("已删除 animation: ma-spin", !/animation:\s*ma-spin/.test(css));
  check("保留静态禁用态（pointer-events:none）", /\.btn-tiny\.spinning\s*\{[^}]*pointer-events:\s*none/.test(css));
  check("ma-refresh 按钮存在", !!q("#ma-refresh"));

  console.log("\n── ④ 行情剧震事件触发 ──");
  // ★ 2026-10-30：直接从 index.html 里抽 script 段（原来读 /root/.hermes/cache/scratch/
  //   idx_script_0.js —— 那是临时目录，scratch 清理后测试就崩，自包含才可靠）
  const src = (html.match(/<script>([\s\S]*?)<\/script>/) || [, ""])[1];
  check("checkShockTrigger 函数已定义", /function checkShockTrigger/.test(src));
  check("剧震轮询 setInterval 已接", /setInterval\(checkShockTrigger/.test(src));
  check("启动时立即调用一次", /^\s*checkShockTrigger\(\);/m.test(src));
  check("前端调 /api/market_shock", /\/api\/market_shock/.test(src));
  check("同波行情只触发一次（_shockFired）", /_shockFired/.test(src));

  console.log("\n── ⑤ 老师单分析初始化 ──");
  check("fetchTeacherAnalysis 已定义", /function fetchTeacherAnalysis/.test(src));
  check("老师单卡定时刷新已接", /setInterval\(fetchTeacherAnalysis/.test(src));
  check("renderTeacherPnl 已定义", /function renderTeacherPnl/.test(src));
  check("renderTeacherEntry 已定义", /function renderTeacherEntry/.test(src));

  dom.window.close();
  console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
  if (failures.length) console.log("失败项:", failures.join(" | "));
  process.exit(fail ? 1 : 0);
})().catch(e => {
  console.error("测试崩溃:", e);
  process.exit(2);
});
