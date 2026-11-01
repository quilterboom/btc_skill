/**
 * 行情大图 · 主图平移/缩放/历史翻页测试（jsdom）
 * 2026-11-01 · liusir：「价格主图滑动的功能怎么没有加」→ 前端接 /api/market_chart 后的回归
 *
 * 覆盖：
 *   1. 深历史序列加载（_chartStore 填充，≥600 根）
 *   2. 视口切片 _chartView（默认 160 根窗口）
 *   3. 拖拽平移（mousedown+mousemove → _rightGap 变化，视口跟着移）
 *   4. 拖到左边界自动翻页（store 增长 + _rightGap 同步增 = 视觉位置不动）
 *   5. 滚轮缩放（wheel → _viewBars 变化）
 *   6. 「回到最新」按钮（_rightGap 归零）
 *   7. 图例提示文案 + 现价线只在贴最新时画（源码断言）
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const BASE = "http://127.0.0.1:23003";
const SRC = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/market.html", "utf8");

let pass = 0, fail = 0;
const failures = [];
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; failures.push(name + (extra ? " → " + extra : "")); console.log("  ✗ " + name + (extra ? " → " + extra : "")); }
}

(async () => {
  const dom = new JSDOM(SRC, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: BASE + "/market",
    beforeParse(window) {
      window.fetch = (url, opts) => fetch(new URL(url, BASE).href, opts);
      window.addEventListener("error", e => {
        console.error("[PAGE ERROR]", e.message, e.error && e.error.stack);
      });
      window.HTMLCanvasElement.prototype.getContext = function () {
        const noop = () => {};
        return {
          setTransform: noop, clearRect: noop, beginPath: noop, moveTo: noop, lineTo: noop,
          stroke: noop, fill: noop, fillRect: noop, fillText: noop, save: noop, restore: noop,
          setLineDash: noop, measureText: () => ({ width: 10 }), rect: noop, arc: noop,
          canvas: this,
        };
      };
      Object.defineProperty(window.HTMLCanvasElement.prototype, "clientWidth", { get() { return 900; } });
      Object.defineProperty(window.HTMLCanvasElement.prototype, "clientHeight", { get() { return 460; } });
    },
  });

  const { window } = dom;
  const doc = window.document;
  const q = s => doc.querySelector(s);
  const ev = (el, type, init) => el.dispatchEvent(new window.MouseEvent(type, Object.assign({ bubbles: true, cancelable: true }, init)));

  // ---- 0. 源码断言 ----
  console.log("【0】源码断言");
  check("引用 /api/market_chart 深历史端点", SRC.includes("/api/market_chart"));
  check("翻页请求带 end 参数（往回翻页语义）", /market_chart\?tf=\$\{tf\}&bars=600&end=/.test(SRC));
  check("绑定 mousedown 拖拽", SRC.includes('_cvEl.addEventListener("mousedown"'));
  check("绑定 wheel 缩放", SRC.includes('_cvEl.addEventListener("wheel"'));
  check("绑定 touchstart/touchmove 触屏滑动",
    SRC.includes('_cvEl.addEventListener("touchstart"') && SRC.includes('_cvEl.addEventListener("touchmove"'));
  check("翻页依据 has_more_before", SRC.includes("has_more_before"));
  check("现价线只在贴最新时画", /if \(data\._hi === data\._total\) \{[\s\S]*?lastC/.test(SRC));

  // ---- 1. 等深历史加载 ----
  // 直接触发 loadChartHistory()，不等 market_board（board 冷启动含 LLM 判读可要 10-20s）
  console.log("【1】深历史序列加载");
  window.eval("loadChartHistory()");
  let ok = false, total0 = 0;
  for (let i = 0; i < 120; i++) {
    total0 = Number(window.eval("(_chartStore['1h'] || {t:[]}).t.length")) || 0;
    if (total0 >= 500) { ok = true; break; }
    await new Promise(r => setTimeout(r, 250));
  }
  check("深历史 1h 已加载 ≥500 根", ok, "实际 " + total0);
  check("窗口端点给的是 600 根窗口", total0 === 600, "实际 " + total0);
  check("has_more_before=true（还能往左翻）",
    window.eval("(_chartStore['1h'] || {}).has_more_before") === true);
  if (!ok) {
    console.log("深历史未加载成功，后续交互测试无意义，提前结束");
    dom.window.close();
    process.exit(1);
  }

  // ---- 2. 视口切片 ----
  console.log("【2】视口切片");
  const vlen = Number(window.eval("_chartView().t.length"));
  check("默认视口 160 根", vlen === 160, "实际 " + vlen);
  check("视口右端贴最新（_rightGap=0）", Number(window.eval("_rightGap")) === 0);

  // ---- 3. 拖拽平移 ----
  console.log("【3】拖拽平移");
  const cv = q("#chart-canvas");
  const tsRight0 = window.eval("_chartView().t.slice(-1)[0]");
  ev(cv, "mousedown", { clientX: 500 });
  ev(window, "mousemove", { clientX: 620 });   // 右拖 120px ≈ 27 根（900px 宽 / 160 根）
  ev(window, "mouseup", {});
  const gap1 = Number(window.eval("_rightGap"));
  check("右拖后 _rightGap > 0（窗口往过去走）", gap1 > 0, "实际 " + gap1);
  const tsRight1 = window.eval("_chartView().t.slice(-1)[0]");
  check("视口末根 ts 前移（真的滑动了）", tsRight1 < tsRight0, `${tsRight1} !< ${tsRight0}`);
  check("拖拽期间图例仍渲染", (q("#chart-legend").textContent || "").length > 20);

  // 左拖回来
  ev(cv, "mousedown", { clientX: 500 });
  ev(window, "mousemove", { clientX: 300 });
  ev(window, "mouseup", {});
  check("左拖后 _rightGap 回落", Number(window.eval("_rightGap")) < gap1,
    "gap1=" + gap1 + " now=" + window.eval("_rightGap"));

  // ---- 4. 拖到左边界自动翻页 ----
  console.log("【4】历史翻页");
  window.eval("_rightGap = 480; drawChart();");
  const gapBeforePage = Number(window.eval("_rightGap"));   // drawChart 归一化后的值
  window.eval("_ensureMoreHistory()");
  let grew = false, total1 = total0;
  for (let i = 0; i < 40; i++) {
    total1 = Number(window.eval("_chartStore['1h'].t.length"));
    if (total1 > total0) { grew = true; break; }
    await new Promise(r => setTimeout(r, 250));
  }
  check("拖到左边界后自动补到更早历史", grew, `total ${total0} → ${total1}`);
  check("翻页后全序列严格升序（头插无缝）",
    window.eval("(function(){const t=_chartStore['1h'].t;for(let i=1;i<t.length;i++){if(t[i]<=t[i-1])return false;}return true;})()"));
  const gapAfter = Number(window.eval("_rightGap"));
  const added = total1 - total0;
  check("翻页后 _rightGap 同步增（视觉位置不动）", gapAfter === gapBeforePage + added,
    `gap ${gapBeforePage} + added ${added} ≠ ${gapAfter}`);

  // ---- 5. 滚轮缩放 ----
  console.log("【5】滚轮缩放");
  const vb0 = Number(window.eval("_viewBars"));
  const wheelUp = new window.Event("wheel", { bubbles: true, cancelable: true });
  wheelUp.deltaY = -120;
  cv.dispatchEvent(wheelUp);
  const vb = Number(window.eval("_viewBars"));
  check("滚轮上滚 → 放大（显示更少根）", vb < vb0, `${vb0} → ${vb}`);
  const wheelDown = new window.Event("wheel", { bubbles: true, cancelable: true });
  wheelDown.deltaY = 120;
  cv.dispatchEvent(wheelDown);
  check("滚轮下滚 → 缩小（显示更多根）", Number(window.eval("_viewBars")) > vb);

  // ---- 6. 回到最新 ----
  console.log("【6】回到最新");
  q("#chart-reset").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  check("点击后 _rightGap 归零", Number(window.eval("_rightGap")) === 0);
  check("点击后视口尾根 = 序列最新根",
    window.eval("_chartView().t.slice(-1)[0]") === window.eval("_chartStore['1h'].t.slice(-1)[0]"));

  // ---- 7. 图例文案 ----
  console.log("【7】图例与文案");
  await new Promise(r => setTimeout(r, 100));
  window.eval("drawChart()");
  const legend = q("#chart-legend").textContent;
  check("图例含「左右拖动回看更早」提示", legend.includes("左右拖动回看更早"), legend);
  check("图例含根数统计", /共 \d+ 根|显示 \d+ \/ 已载 \d+ 根/.test(legend), legend);
  check("图例无 undefined/NaN 泄漏", !/undefined|NaN/.test(legend), legend);
  check("「回到最新」按钮存在", q("#chart-reset") !== null);

  console.log(`\n═══ 结果：${pass} 通过 / ${fail} 失败 ═══`);
  if (failures.length) console.log("失败项：\n  - " + failures.join("\n  - "));
  dom.window.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("测试崩溃:", e); process.exit(1); });
