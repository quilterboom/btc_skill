/**
 * 训练页排版对齐正式环境（2026-10-30 liusir 要求）
 *
 * 需求：「修改训练页面的数据展示排版，与正式的排版一致，当然，
 *       如果训练环境没有的数据那么就不用展示」
 *
 * ★ jsdom 局限（实测）：跨 realm 的 fetch 只有 JSDOM 构造期第一次能 resolve，
 *   之后 window 内发起的 fetch 全部卡住（node 主上下文 fetch 200、window.eval TIMEOUT）。
 *   所以**自动 load() 链路在 jsdom 里走不通**，测试改为：
 *     主上下文 node fetch 取真实 API → window.eval 显式喂 renderTraining() 验渲染层。
 *   自动链路的正确性（load 按 _env 选端点、render 按 _env 分流）用源码断言覆盖。
 *
 * 验证要点：
 *  ① 结构同构：sec-train-overview 4 卡 / sec-train-strategy 全宽 / sec-train-analysis data-grid
 *  ② 训练没数据的卡（Edge Score / P&L 日历 / 错误归因）**不存在**，不留「加载中」死壳
 *  ③ renderTraining 把真实 API 数据填进各容器
 *  ④ 死壳字段（train-hb-niter/done/still）已删 —— API 根本不返回这些
 *  ⑤ 指标中文化（止损/止盈一/止盈二，不是 SL/TP1/TP2）
 *  ⑥ CSS：概览强制 4 列、数据分析 2 列铺满
 */
const { JSDOM } = require("/root/node_modules/jsdom");
const fs = require("fs");
const path = require("path");

const HTML_PATH = path.join(__dirname, "..", "index.html");
const BASE = "http://127.0.0.1:23003";
let pass = 0, fail = 0;
function check(name, ok, extra = "") {
  if (ok) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ${name}  ${extra ? "(" + extra + ")" : ""}`); }
}

(async () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");

  // ── 真实 API：训练环境数据（主上下文 fetch，可靠）──
  const trainData = await fetch(BASE + "/api/status/training").then(r => r.json());

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    resources: "usable",
    url: BASE + "/",
    beforeParse(window) {
      window.fetch = (u, o) => fetch(new URL(u, BASE).href, o);
      window.alert = () => {}; window.prompt = () => null; window.confirm = () => true;
    },
  });
  const { window } = dom;
  const doc = window.document;
  const q = (sel) => doc.querySelector(sel);
  const qa = (sel) => [...doc.querySelectorAll(sel)];

  // 切到训练环境（验证 switchEnv 的 DOM 状态切换）
  window.switchEnv("training");
  await new Promise(r => setTimeout(r, 300));

  // ★ 显式喂真实数据给 renderTraining（绕开 jsdom 的 fetch 局限）
  //   用 window.eval 把数据传进 vm 上下文，再调 renderTraining
  window.eval(`window.__TRAIN_DATA = ${JSON.stringify(trainData)};`);
  window.eval("renderTraining(window.__TRAIN_DATA)");
  await new Promise(r => setTimeout(r, 100));

  console.log("\n── ① 结构同构（与 prod 的 sec-overview / sec-strategy / sec-analysis 同排版）──");
  check("sec-train-overview 存在且带 .grid", !!q("#sec-train-overview.grid"));
  check("sec-train-overview 有 sec-hd 标题", !!q("#sec-train-overview .sec-hd"));
  check("sec-train-overview 有 4 张 .card", qa("#sec-train-overview > .card").length === 4,
    `实际 ${qa("#sec-train-overview > .card").length}`);
  check("sec-train-strategy 是全宽 .grid + .card", !!q("#sec-train-strategy.grid > .card"));
  check("sec-train-analysis 是 .data-grid", !!q("#sec-train-analysis.data-grid"));
  check("sec-train-analysis 有 sec-hd 标题", !!q("#sec-train-analysis .sec-hd"));
  check("sec-train-analysis 有 2 张 .data-card", qa("#sec-train-analysis > .data-card").length === 2,
    `实际 ${qa("#sec-train-analysis > .data-card").length}`);
  check("老的 split-grid 双栏已移除（panel-train-strategy）", !q("#panel-train-strategy"));
  check("老的 panel-train-effect 已移除", !q("#panel-train-effect"));
  check("辅助区 train-aux 是折叠 details", !!q("details.train-aux#train-aux"));
  check("train-aux 默认不展开（无 open 属性）", !q("details.train-aux").hasAttribute("open"));

  console.log("\n── ② 训练环境没数据的卡不展示（不留「加载中」死壳）──");
  check("Edge Score 卡已删", !q("#train-edge-score") && !/train-edge-score/.test(html));
  check("P&L 日历卡已删", !q("#train-pnl-calendar") && !/train-pnl-calendar/.test(html));
  check("错误归因卡已删", !q("#train-mistakes") && !/train-mistakes/.test(html));
  check("renderTraining 不再调 renderEdgeScore", !/renderEdgeScore\(d\.edge_score,\s*"train-/.test(html));
  check("renderTraining 不再调 renderPnlCalendar", !/renderPnlCalendar\(d\.pnl_calendar,\s*"train-/.test(html));
  check("renderTraining 不再调 renderMistakes", !/renderMistakes\(d\.mistakes,\s*"train-/.test(html));
  check("页面 body 里无「加载中」残留死壳（训练容器已填）",
    qa("#sec-train-overview, #sec-train-analysis").every(s => s.innerHTML.indexOf("加载中") === -1));

  console.log("\n── ③ renderTraining 把真实 API 数据填进各容器 ──");
  const n = (v) => (v == null ? 0 : Number(v));
  const counters = trainData.settler_counters || {};
  const st = trainData.strategy || {};

  const procPill = q("#train-proc-pill");
  check("训练进程 pill 已填", procPill && procPill.innerHTML.length > 5,
    procPill ? procPill.innerHTML : "未找到");
  check("进程 pill 有 ok/warn 状态类",
    procPill && /(pill ok|pill warn)/.test(procPill.innerHTML));

  const hbAge = q("#train-hb-age");
  check("settler 心跳已填（非 —）", hbAge && hbAge.textContent !== "—",
    hbAge ? hbAge.textContent : "未找到");

  const settleBig = q("#train-settle-big");
  const expectNet = n(counters.net_usd).toFixed(2);
  check("训练结算大字 === API net_usd", settleBig && settleBig.textContent.includes(expectNet),
    settleBig ? `渲染=${settleBig.textContent} API=${expectNet}` : "未找到");
  check("净利大字染色（正绿/负红）",
    settleBig && /var\(--green\)|var\(--red\)/.test(settleBig.style.color || ""));

  check("总单数 === API strategy.total",
    q("#train-settle-n").textContent === String(st.total),
    `渲染=${q("#train-settle-n").textContent} API=${st.total}`);
  check("已结算 === API settler_counters.settled",
    q("#train-settle-settled").textContent === String(counters.settled),
    `渲染=${q("#train-settle-settled").textContent} API=${counters.settled}`);
  check("待结算 === API settler_counters.pending",
    q("#train-settle-pending").textContent === String(counters.pending),
    `渲染=${q("#train-settle-pending").textContent} API=${counters.pending}`);

  const btcPx = q("#train-btc-price");
  check("BTC 现价已填（含 $）", btcPx && /\$[\d,]/.test(btcPx.textContent),
    btcPx ? btcPx.textContent : "未找到");
  const btcMeta = q("#train-btc-meta");
  check("BTC meta 含 24h 涨跌", btcMeta && /24h/.test(btcMeta.textContent),
    btcMeta ? btcMeta.textContent : "未找到");
  check("BTC meta 含资金费率", btcMeta && /资金费率/.test(btcMeta.textContent));

  const active = q("#train-active");
  check("当前持仓卡已填（有持仓或空态）",
    active && (active.innerHTML.includes("当前无活跃持仓") || active.innerHTML.length > 50),
    active ? active.innerHTML.slice(0, 80) : "未找到");

  check("策略摘要行已填（含共 X 单）",
    q("#train-strategy-summary") && /共\s*<b>\d+<\/b>\s*单/.test(q("#train-strategy-summary").innerHTML),
    q("#train-strategy-summary") ? q("#train-strategy-summary").innerHTML.slice(0, 60) : "未找到");

  const matrix = q("#train-strategy-matrix");
  check("策略-效果矩阵已填（非加载中）",
    matrix && matrix.innerHTML.indexOf("加载中") === -1 && matrix.innerHTML.length > 50);
  const recent = q("#train-recent-trades");
  check("最近 10 笔已填（非加载中）",
    recent && recent.innerHTML.indexOf("加载中") === -1 && recent.innerHTML.length > 50);

  console.log("\n── ④ 死壳字段已删（API 根本不返回这些）──");
  check("train-hb-niter DOM 不存在", !q("#train-hb-niter"));
  check("train-hb-done DOM 不存在", !q("#train-hb-done"));
  check("train-hb-still DOM 不存在", !q("#train-hb-still"));
  check("train-strategy-overview DOM 不存在", !q("#train-strategy-overview"));
  check("train-effect-meta DOM 不存在", !q("#train-effect-meta"));

  console.log("\n── ⑤ 指标中文化（用户可见标签不许英文缩写）──");
  const activeHtml = active ? active.innerHTML : "";
  const hasPosition = activeHtml.indexOf("当前无活跃持仓") === -1 && activeHtml.length > 50;
  if (hasPosition) {
    check("持仓卡用「止损」不是 SL", /止损/.test(activeHtml) && !/>\s*SL\s*</.test(activeHtml));
    check("持仓卡用「止盈一」不是 TP1", /止盈一/.test(activeHtml) && !/>\s*TP1\s*</.test(activeHtml));
    check("持仓卡用「止盈二」不是 TP2", /止盈二/.test(activeHtml) && !/>\s*TP2\s*</.test(activeHtml));
  } else {
    // 当前无活跃持仓 → 行没渲染，改断言渲染模板源码用的是中文标签
    check("持仓卡渲染模板用「止损」（非 SL）", /<span class="lbl">止损<\/span>/.test(html));
    check("持仓卡渲染模板用「止盈一」（非 TP1）", /<span class="lbl">止盈一<\/span>/.test(html));
    check("持仓卡渲染模板用「止盈二」（非 TP2）", /<span class="lbl">止盈二<\/span>/.test(html));
    check("持仓卡当前显示空态文案", /当前无活跃持仓/.test(activeHtml));
  }
  check("页面无裸 WR/DD/PF/ES 指标标签",
    !/>\s*(WR|DD|PF|ES)\s*</.test(doc.body.innerHTML));

  console.log("\n── ⑥ CSS：铺满规则 ──");
  const css = html.match(/<style>([\s\S]*?)<\/style>/)?.[1] || "";
  check("CSS 强制训练概览 4 列",
    /#sec-train-overview\.grid\s*\{[^}]*repeat\(4,\s*1fr\)/.test(css));
  check("CSS 训练数据分析 2 列铺满",
    /#sec-train-analysis\.data-grid\s*\{[^}]*grid-template-columns:\s*1fr 1fr/.test(css));
  check("CSS 窄屏退化规则存在",
    /@media \(max-width: 1100px\)[^}]*#sec-train-overview/.test(css));

  console.log("\n── ⑦ 自动链路正确性（源码断言，jsdom fetch 局限无法实跑）──");
  check("load() 按 _env 选训练端点",
    /const url = _env === "training" \? "\/api\/status\/training" : "\/api\/status"/.test(html));
  check("render() 按 _env 分流到 renderTraining",
    /if \(_env === "training"\) return renderTraining\(d\)/.test(html));
  check("switchEnv() 会调 load() 触发刷新",
    /function switchEnv\([\s\S]{0,1500}?\n\s*load\(\);/.test(html));
  check("switchEnv 切 body[data-env]",
    /document\.body\.dataset\.env = env/.test(html));

  console.log("\n── ⑧ 训练数据确实不含 ES/P&L/错误（印证「不展示」是对的）──");
  check("API 不返回 edge_score", trainData.edge_score === undefined);
  check("API 不返回 pnl_calendar", trainData.pnl_calendar === undefined);
  check("API 不返回 mistakes", trainData.mistakes === undefined);

  console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
  dom.window.close();
  process.exit(fail > 0 ? 1 : 0);
})().catch(e => { console.error("ERROR:", e); process.exit(1); });
