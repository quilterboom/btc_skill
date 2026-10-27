/**
 * 行情大图 DOM 功能测试（jsdom）
 * 2026-10-27 · 验证 /market 页面渲染是否正确
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

(async () => {
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: BASE + "/market",
    beforeParse(window) {
      window.fetch = (url, opts) => fetch(new URL(url, BASE).href, opts);
      // 捕获页面内异常（渲染中途抛错会导致后半部分区块空白）
      window.addEventListener("error", e => {
        console.error("[PAGE ERROR]", e.message, e.error && e.error.stack);
      });
      const origErr = window.console.error;
      window.console.error = (...a) => { origErr("[console.error]", ...a); };
      const origWarn = window.console.warn;
      window.console.warn = (...a) => { origWarn("[console.warn]", ...a); };
      // canvas 在 jsdom 里没有实现 → 给个最小 stub，避免 drawChart 抛异常
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
  const qa = s => Array.from(doc.querySelectorAll(s));

  // 等首屏数据渲染完：轮询「全部 5 个区块都非空」，而不是只等现价
  // （只等现价会误判：renderBoard 里若中途抛错，现价已填但后面区块仍空白）
  const allReady = () => {
    const px = q("#px-last");
    return px && px.textContent !== "—"
      && qa(".tf-row").length === 3
      && qa(".lv-table tbody tr").length > 0
      && (q(".ai-body") && q(".ai-body").textContent.length > 30);
  };
  let ready = false;
  for (let i = 0; i < 60; i++) {
    if (allReady()) { ready = true; break; }
    await new Promise(r => setTimeout(r, 250));
  }
  check("首屏 5 个区块全部渲染完成", ready, "超时 15s 未就绪");

  console.log("\n【1】环境结论区");
  const pxLast = q("#px-last").textContent;
  check("现价已渲染（$ + 数字）", /^\$[\d,]+\.\d{2}$/.test(pxLast), pxLast);
  check("涨跌已渲染", q("#px-chg").textContent.includes("24 小时"), q("#px-chg").textContent);
  check("24h 低已渲染", /24h 低 [\d,]+/.test(q("#range-lo").textContent), q("#range-lo").textContent);
  check("24h 高已渲染", /24h 高 [\d,]+/.test(q("#range-hi").textContent), q("#range-hi").textContent);
  check("区间位置已渲染", /区间位置 \d+%/.test(q("#range-pos").textContent), q("#range-pos").textContent);

  const badge = q("#verdict-badge");
  check("方向徽章有中文（偏多/偏空/中性）", /偏多|偏空|中性/.test(badge.textContent), badge.textContent);
  check("方向徽章带一致性副标题", badge.querySelector(".sub") !== null, badge.innerHTML);
  const fillW = q("#norm-fill").style.width;
  check("归一化得分条有宽度", fillW && fillW !== "0%", "width=" + fillW);
  const markerL = q("#norm-marker").style.left;
  check("归一化指针有位置", markerL && markerL !== "50%", "left=" + markerL);

  console.log("\n【2】指标小卡（4 张）");
  const tiles = qa(".tile");
  check("4 张指标卡", tiles.length === 4, "实际 " + tiles.length);
  check("波动率卡有 %", q("#t-vol").textContent.includes("%"), q("#t-vol").textContent);
  check("资金费率卡有 %", q("#t-fund").textContent.includes("%"), q("#t-fund").textContent);
  check("量能卡有倍数", q("#t-vol-2").textContent.includes("×"), q("#t-vol-2").textContent);
  check("闸门卡是放行/挡掉", /放行|挡掉/.test(q("#t-gate").textContent), q("#t-gate").textContent);
  check("闸门卡写了门槛值", q("#t-gate-sub").textContent.includes("门槛"), q("#t-gate-sub").textContent);

  console.log("\n【3】多周期矩阵");
  const rows = qa(".tf-row");
  check("3 个周期行", rows.length === 3, "实际 " + rows.length);
  const names = qa(".tf-name").map(e => e.textContent.trim());
  check("周期名是中文（4 小时/1 小时/15 分钟）",
    names.join("|") === "4 小时|1 小时|15 分钟", names.join("|"));
  check("每行有趋势标签", qa(".tf-row .tag").length >= 9, "tag 数 " + qa(".tf-row .tag").length);
  check("每行有 3 个维度条", qa(".dim").length === 9, "dim 数 " + qa(".dim").length);
  check("维度条有比例宽度", qa(".dim .d-bar i").every(e => /%$/.test(e.style.width || "")),
    qa(".dim .d-bar i").map(e => e.style.width).join(","));

  console.log("\n【4】关键位");
  const lvRows = qa(".lv-table tbody tr");
  check("关键位有数据行", lvRows.length >= 4, "实际 " + lvRows.length);
  check("关键位区分阻力/支撑", qa(".lv-tag.res").length >= 1 && qa(".lv-tag.sup").length >= 1,
    "res=" + qa(".lv-tag.res").length + " sup=" + qa(".lv-tag.sup").length);
  check("关键位有距现价百分比", qa(".lv-table td.r").some(e => e.textContent.includes("%")));

  console.log("\n【5】交易员判读");
  const ai = q(".ai-body");
  check("判读有正文", ai && ai.textContent.length > 30, "长度 " + (ai ? ai.textContent.length : 0));
  check("判读有多行保留（white-space: pre-wrap）",
    window.getComputedStyle(ai).whiteSpace === "pre-wrap", window.getComputedStyle(ai).whiteSpace);
  check("判读标注了来源", /AI 模型|规则/.test(q(".foot-note").textContent), q(".foot-note").textContent.slice(0, 60));

  console.log("\n【6】主图");
  check("canvas 存在", q("#chart-canvas") !== null);
  const legend = q("#chart-legend").textContent;
  check("图例含 EMA20", legend.includes("EMA20"), legend);
  check("图例含 EMA50", legend.includes("EMA50"), legend);
  check("图例写了当前周期结构", /趋势/.test(legend), legend);
  check("周期切换有 3 个 tab", qa("#tf-tabs button").length === 3);

  // 切换周期
  const before = q("#chart-legend").textContent;
  qa("#tf-tabs button").find(b => b.dataset.tf === "4h").click();
  await new Promise(r => setTimeout(r, 200));
  const after = q("#chart-legend").textContent;
  check("切到 4 小时后图例刷新", after !== before && after.includes("4 小时"), after);
  check("4h tab 变 active", qa("#tf-tabs button").find(b => b.dataset.tf === "4h").classList.contains("active"));

  console.log("\n【7】中文化（用户可见标签不得出现英文缩写）");
  const banned = /\b(WR|DD|PF|ES|PnL|OI|SR|TP1|TP2|SL|Long|Short)\b/;
  const visibleText = q(".wb-content").textContent;
  const bannedHit = visibleText.match(banned);
  check("主视图无英文指标缩写", bannedHit === null, bannedHit ? bannedHit.join(",") : "无");
  check("维度标签是中文（趋势/动能/波动）",
    visibleText.includes("趋势") && visibleText.includes("动能") && visibleText.includes("波动"));

  console.log("\n【8】无残留占位/错误");
  check("没有加载中残留", !visibleText.includes("加载中"), "有");
  check("没有 undefined 泄漏", !visibleText.includes("undefined"), visibleText.match(/.{0,30}undefined.{0,30}/)?.[0]);
  check("没有 NaN 泄漏", !visibleText.includes("NaN"), visibleText.match(/.{0,30}NaN.{0,30}/)?.[0]);
  check("没有 [object Object]", !visibleText.includes("[object Object]"));

  console.log("\n【9】侧栏导航锚点齐全");
  const anchors = qa(".wb-nav a").map(a => a.getAttribute("href"));
  const targets = anchors.map(h => h.replace("#", ""));
  check("锚点都有对应元素", targets.every(t => doc.getElementById(t) !== null), targets.join(","));
  check("有返回工作台链接", qa(".wb-side-foot a").some(a => a.getAttribute("href") === "/"));

  console.log("\n【10】周期切换不报错");
  let err = null;
  try {
    window.eval("switchTf('15m')");
    await new Promise(r => setTimeout(r, 200));
    window.eval("switchTf('4h')");
    window.eval("switchTf('1h')");
  } catch (e) { err = e; }
  check("三个周期切换无异常", err === null, err ? err.message : "无");

  console.log(`\n═══ 结果：${pass} 通过 / ${fail} 失败 ═══`);
  if (failures.length) {
    console.log("失败项：");
    failures.forEach(f => console.log("  · " + f));
  }
  process.exit(fail ? 1 : 0);
})();
