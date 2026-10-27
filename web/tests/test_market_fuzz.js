/**
 * 行情大图 · 变异测试（fuzz）
 *
 * 定位「renderLevels 抛错导致后续区块空白」的确切触发条件。
 *
 * 方法：拿真实 /api/market_board 载荷，逐字段随机置 null / 删除 / 改成错误类型，
 *       跑每个 render 函数，看哪个变异会让它抛错。
 *       抛错 = 真实线上事故的候选根因。
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const html = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/market.html", "utf8");
const BASE = "http://127.0.0.1:23003";

const FNS = ["renderVerdict", "renderTiles", "renderTfMatrix", "renderLevels", "renderAdvisory"];

function deepClone(o) { return JSON.parse(JSON.stringify(o)); }

// 变异操作
const MUTATIONS = [
  ["null", v => null],
  ["delete", v => undefined],
  ["nan", v => NaN],
  ["emptyObj", v => ({})],
];

(async () => {
  const real = await fetch(BASE + "/api/market_board").then(r => r.json());
  if (real.error) { console.log("API error:", real.error); process.exit(1); }

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: BASE + "/market",
    beforeParse(w) {
      w.fetch = () => Promise.reject(new Error("offline"));
      w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {} });
      Object.defineProperty(w.HTMLCanvasElement.prototype, "clientWidth", { get() { return 900; } });
      Object.defineProperty(w.HTMLCanvasElement.prototype, "clientHeight", { get() { return 460; } });
    },
  });
  const w = dom.window;

  const throws = [];   // {path, mut, fn, msg}
  const paths = [];

  // 枚举所有「叶子字段路径」——但跳过 chart.* 的大数组（它们没有分支逻辑，
  // 且会让变异次数爆炸）
  (function walk(obj, prefix, depth) {
    if (depth > 4 || obj === null || typeof obj !== "object") return;
    for (const k of Object.keys(obj)) {
      const v = obj[k];
      const p = prefix ? `${prefix}.${k}` : k;
      if (p.startsWith("chart.")) continue;      // 画图数据无分支逻辑，跳过
      if (v !== null && typeof v === "object") {
        paths.push(p);
        walk(v, p, depth + 1);
      } else {
        paths.push(p);
      }
    }
  })(real, "", 0);

  const setPath = (obj, path, val) => {
    const parts = path.split(".");
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) cur = cur[parts[i]];
    cur[parts[parts.length - 1]] = val;
  };

  let tested = 0;
  for (const path of paths) {
    for (const [mutName, mutFn] of MUTATIONS) {
      const payload = deepClone(real);
      try { setPath(payload, path, mutFn()); } catch (e) { continue; }
      tested++;
      for (const fn of FNS) {
        w.payload = payload;
        try {
          w.eval(`${fn}(payload)`);
        } catch (e) {
          throws.push({ path, mut: mutName, fn, msg: e.message });
        }
      }
    }
  }

  console.log(`变异测试：${paths.length} 条路径 × ${MUTATIONS.length} 种变异 × ${FNS.length} 个函数 = ${tested * FNS.length} 次调用`);
  console.log(`抛错次数：${throws.length}`);
  if (throws.length) {
    // 按 (fn, path) 聚合
    const agg = new Map();
    for (const t of throws) {
      const key = `${t.fn} | ${t.path}`;
      if (!agg.has(key)) agg.set(key, { ...t, count: 0, muts: [] });
      const e = agg.get(key);
      e.count++;
      e.muts.push(t.mut);
    }
    console.log("\n按「函数 + 字段」聚合：");
    for (const [k, v] of agg) {
      console.log(`  ${k.padEnd(50)} × ${v.count}  [${[...new Set(v.muts)].join(",")}]  ${v.msg}`);
    }
  } else {
    console.log("\n✓ 所有变异都未触发抛错");
  }

  dom.window.close();
  process.exit(throws.length ? 1 : 0);
})();
