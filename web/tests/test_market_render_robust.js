/**
 * 行情大图 · 渲染健壮性测试
 *
 * 用一批「畸形 / 边界」payload 直接调用页面的各个 render 函数，
 * 找出哪些输入形状会让渲染抛错。
 *
 * 背景：曾出现间歇性失败——首屏「多周期」有数据，但「关键位」和「交易员判读」空白，
 * 症状是 renderLevels 抛错把 renderBoard 后半段截断了。
 * 这个测试把所有可能的 payload 形状都跑一遍，确保任何一个都不抛错。
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const html = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/market.html", "utf8");

const TF_OK = {
  trend: "空头排列", trend_score: -1, ema20: 80100, ema50: 80300, ema200: 81000,
  macd_state: "多头", macd_score: 1, rsi: 43.4, rsi_state: "中性",
  atr: 400, atr_pct: 0.49, atr_regime: "正常波动", atr_ratio: 1.12,
  bb_state: "中轨附近", bb_position: 0.5, bb_bandwidth: 1.2,
  kc_state: "通道内", kc_squeeze: "未收敛",
  vol_regime: "温和", vol_ratio: 1.12,
  structure: "下跌段·末段", structure_bias: "neutral", pos_in_leg: 0.7,
  swing_note: "距 0.382 反弹位 80,360.4 -107 点 (-0.13%)",
  swing_kind: "down", swing_hi: 80800, swing_lo: 79430,
  retracements: { "0.236": 79964.34, "0.382": 80360.43, "0.5": 80680.55 },
  session: "亚盘主段 / 欧盘衔接",
  score_trend: -1, score_momentum: -2, score_volatility: 0,
  px: 80100, n_bars: 240, first_ts: "10-01 00:00", last_ts: "10-27 08:00",
  high_period: 85000, low_period: 79000, px_vs_ema20: 0.1,
};

const PAYLOADS = {
  normal: {
    price: { last: 80000, high_24h: 80800, low_24h: 79324, chg_24h_pct: 0.23,
              pos_in_range_pct: 60.4, volume_24h: 638716846, open_interest: 565983272 },
    tfs: { "4h": { ...TF_OK }, "1h": { ...TF_OK, swing_kind: "up" }, "15m": { ...TF_OK } },
    aggregate: { bias: "neutral", norm: -0.1, score: -11.5, consensus: "弱一致（含中性）",
                 consensus_score: 0.5, rows: [{ tf: "4h", weight: 3 }] },
    volatility: { atr_pct: 0.49, atr_regime: "normal", atr_ratio: 1.12 },
    funding: { current: 0.0001, previous: 0.000085, sign: "neutral" },
    gate: { ok: true, vol_5d_pct: 0.436, vol_min_pct: 0.65, vol_pass: false, blocked_24h: 0 },
    chart: { "1h": { t: [1, 2], o: [1, 2], h: [2, 3], l: [0, 1], c: [1.5, 2.5], v: [10, 20],
                     ema20: [1, 1.5], ema50: [null, 1.2], rsi: [50, 51] } },
    advisory: { text: "判读内容".repeat(20), color: "neu", risk: "正常波动",
                verdict: "中性", text_source: "mimo" },
  },
  tf_missing: { price: { last: 80000 }, tfs: {}, aggregate: {}, volatility: {},
                funding: {}, gate: {}, chart: {} },
  tf_error: { price: { last: 80000 },
              tfs: { "1h": { error: "K 线获取失败" }, "4h": { error: "x" }, "15m": { error: "y" } },
              aggregate: {}, volatility: {}, funding: {}, gate: {}, chart: {} },
  null_fields: { price: { last: 80000 },
                  tfs: { "1h": { retracements: null, swing_hi: null, swing_lo: null,
                                score_trend: null, score_momentum: null, score_volatility: null,
                                swing_kind: null, trend: null, macd_state: null, rsi: null,
                                rsi_state: null, atr_pct: null, atr_regime: null, atr_ratio: null,
                                vol_regime: null, vol_ratio: null, structure: null,
                                structure_bias: null, ema20: null, ema50: null,
                                session: null, swing_note: null },
                         "4h": { retracements: null, swing_kind: null },
                         "15m": {} },
                  aggregate: { rows: null, bias: null, norm: null, score: null, consensus: null },
                  volatility: {}, funding: {}, gate: {}, chart: {} },
  no_price: { tfs: {}, aggregate: {}, volatility: {}, funding: {}, gate: {}, chart: {} },
  empty_everything: {},
  chart_empty: { price: { last: 80000 }, tfs: { "1h": {}, "4h": {}, "15m": {} },
                 aggregate: {}, volatility: {}, funding: {}, gate: {}, chart: { "1h": {} } },
  chart_partial: { price: { last: 80000 }, tfs: { "1h": {}, "4h": {}, "15m": {} },
                    aggregate: {}, volatility: {}, funding: {}, gate: {},
                    chart: { "1h": { t: [1], o: [1], h: [2], l: [0], c: [1.5], v: [10],
                                     ema20: [null], ema50: [null], rsi: [null] } } },
  advisory_missing: { price: { last: 80000 }, tfs: { "1h": {}, "4h": {}, "15m": {} },
                      aggregate: {}, volatility: {}, funding: {}, gate: {}, chart: {} },
  gate_failed: { price: { last: 80000 }, tfs: { "1h": {}, "4h": {}, "15m": {} },
                  aggregate: {}, volatility: {}, funding: {},
                  gate: { ok: false, error: "闸门读取失败" }, chart: {} },
  funding_missing: { price: { last: 80000 }, tfs: { "1h": {}, "4h": {}, "15m": {} },
                      aggregate: {}, volatility: {}, gate: {}, chart: {} },
};

let fail = 0;
const failures = [];

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://127.0.0.1:23003/market",
  beforeParse(w) {
    w.fetch = () => Promise.reject(new Error("offline test"));
    w.HTMLCanvasElement.prototype.getContext = function () {
      const noop = () => {};
      return { setTransform: noop, clearRect: noop, beginPath: noop, moveTo: noop, lineTo: noop,
               stroke: noop, fill: noop, fillRect: noop, fillText: noop, save: noop, restore: noop,
               setLineDash: noop, measureText: () => ({ width: 10 }) };
    };
    Object.defineProperty(w.HTMLCanvasElement.prototype, "clientWidth", { get() { return 900; } });
    Object.defineProperty(w.HTMLCanvasElement.prototype, "clientHeight", { get() { return 460; } });
  },
});
const w = dom.window;

for (const [name, payload] of Object.entries(PAYLOADS)) {
  const res = {};
  // payload 必须挂到 window 作用域，w.eval 里才能引用到
  w.payload = payload;
  for (const fn of ["renderVerdict", "renderTiles", "renderTfMatrix", "renderLevels", "renderAdvisory"]) {
    try {
      w.eval(`${fn}(payload)`);
      res[fn] = "ok";
    } catch (e) {
      res[fn] = "THROW: " + e.message;
      fail++;
      failures.push(`${name}.${fn} → ${e.message}`);
    }
  }
  // 整链渲染：任何一个区块抛错都不能让后面的区块空白
  try {
    w.eval("renderBoard(payload)");
    res.renderBoard = "ok";
  } catch (e) {
    res.renderBoard = "THROW: " + e.message;
    fail++;
    failures.push(`${name}.renderBoard → ${e.message}`);
  }
  console.log(`${name.padEnd(18)} ${JSON.stringify(res)}`);
}

// renderBoard 之后：5 个区块都必须有内容（畸形数据也要显示占位，不能空白）
console.log("\n【畸形数据下各区块是否都有内容】");
const doc = w.document;
const hosts = { "环境结论": "verdict-meta", "指标卡": "t-vol", "多周期": "tf-matrix",
                "关键位": "levels-body", "交易员判读": "advisory-body" };
for (const [label, id] of Object.entries(hosts)) {
  const el = doc.getElementById(id);
  const txt = el ? el.textContent.trim() : "";
  const ok = txt.length > 0;
  if (!ok) { fail++; failures.push(`畸形数据下 ${label} 区块为空`); }
  console.log(`  ${ok ? "✓" : "✗"} ${label.padEnd(10)} ${txt.slice(0, 48)}`);
}

console.log(`\n═══ 结果：${fail === 0 ? "全部通过" : fail + " 个问题"} ═══`);
failures.forEach(f => console.log("  · " + f));

// jsdom 的 setInterval 会挂住事件循环 → 必须显式 close
dom.window.close();
process.exit(fail ? 1 : 0);
