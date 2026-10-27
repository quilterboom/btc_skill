/**
 * 行情大图 · 时序采样器（抓「半渲染」瞬态）
 *
 * 背景：曾出现间歇性失败——首屏「多周期」有 3 行，但每行的维度条（.dim）为 0，
 *       同时「关键位」「交易员判读」空白。
 *       症状说明：某个 render 函数抛错，把 renderBoard 后半段截断了；
 *       而 .dim 是 renderTfMatrix 内部 _dimBar 生成的 → 抛错点在 _dimBar 上游。
 *
 * 这个采样器每 16ms 抓一次 DOM 状态，记录所有出现过的 (rows, dims, lvRows, aiLen) 组合，
 * 找出不一致的瞬态 → 直接定位是哪个区块先坏。
 */
const fs = require("fs");
const { JSDOM } = require("/root/node_modules/jsdom");

const html = fs.readFileSync("/root/skills/btc-gate-intraday-strategy/web/market.html", "utf8");
const BASE = "http://127.0.0.1:23003";
const RUNS = Number(process.argv[2] || 6);
const SAMPLES = 900;      // 900 × 16ms ≈ 14s，覆盖一次 30s 刷新的一半
const STEP = 16;

(async () => {
  const seen = new Map();     // "rows,dims,lv,ai" → 出现次数
  const anomalies = [];       // 半渲染瞬态

  for (let run = 0; run < RUNS; run++) {
    const dom = new JSDOM(html, {
      runScripts: "dangerously",
      pretendToBeVisual: true,
      url: BASE + "/market",
      beforeParse(w) {
        w.fetch = (u, o) => fetch(new URL(u, BASE).href, o);
        w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {} });
        Object.defineProperty(w.HTMLCanvasElement.prototype, "clientWidth", { get() { return 900; } });
        Object.defineProperty(w.HTMLCanvasElement.prototype, "clientHeight", { get() { return 460; } });
      },
    });
    const doc = dom.window.document;

    for (let i = 0; i < SAMPLES; i++) {
      await new Promise(r => setTimeout(r, STEP));
      let rows = 0, dims = 0, lv = 0, ai = 0, px = "";
      try {
        rows = doc.querySelectorAll(".tf-row").length;
        dims = doc.querySelectorAll(".dim").length;
        lv = doc.querySelectorAll(".lv-table tbody tr").length;
        const aiEl = doc.querySelector(".ai-body");
        ai = aiEl ? aiEl.textContent.length : -1;
        const pxEl = doc.getElementById("px-last");
        px = pxEl ? pxEl.textContent : "";
      } catch (e) { break; }   // window 已关

      const key = `${rows},${dims},${lv},${ai}`;
      seen.set(key, (seen.get(key) || 0) + 1);

      // 半渲染判定：现价已出（说明 renderBoard 已开始）但某些区块还空
      if (px && px !== "—" && (rows === 0 || dims === 0 || lv === 0)) {
        const sig = `${rows},${dims},${lv},${ai}`;
        if (!anomalies.some(a => a.sig === sig)) {
          anomalies.push({ sig, run, px, msg: doc.body.textContent.includes("渲染失败")
            ? (doc.body.textContent.match(/[^。]{0,60}渲染失败[^。]{0,60}/) || [""])[0]
            : "" });
        }
      }
    }
    try { dom.window.close(); } catch (e) {}
  }

  console.log("采样到的 DOM 状态组合 (tf-row数, dim数, 关键位行数, 判读长度):");
  [...seen.entries()].sort((a, b) => b[1] - a[1]).forEach(([k, v]) => {
    const [r, d, l, a] = k.split(",");
    const bad = (r > 0 && d === 0) || (r > 0 && l === 0) || (a >= 0 && a < 30 && r > 0);
    console.log(`  ${bad ? "⚠ " : "  "}${k.padEnd(18)} × ${v}`);
  });
  console.log("\n半渲染瞬态:", anomalies.length);
  anomalies.forEach(a => console.log("  ", JSON.stringify(a)));

  process.exit(0);
})();
