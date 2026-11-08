#!/usr/bin/env node
// 交易收益（周/月/年/总计）卡片 DOM 渲染回归测试
// 2026-11-08 liusir：「修改下前端展示，把交易收益分为周，月，年还有一个总计」
// 锁住：4 行表 / 总计行与胜率卡同数 / 0 笔显示「—」不是 +0.00U / 指标全中文。
// 跑法：node tests/test_pnl_periods_dom.js   （工作目录 = web/）

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const dom = new JSDOM(html, { runScripts: "dangerously", url: "http://localhost/" });
const w = dom.window;

let pass = 0, fail = 0;
function check(name, cond, extra = "") {
  if (cond) { console.log("  ✓ " + name); pass++; }
  else { console.log("  ✗ " + name + (extra ? ` (${extra})` : "")); fail++; }
}

const FILL = {
  basis: "账本可信口径",
  n_settled: 47,
  n_excluded: 16,
  periods: [
    { key: "week",  label: "本周", from: "2026-11-02", to: "2026-11-08", scope: "周一至今",  net_usd: -87.07, n_trades: 4,  wins: 0,  losses: 4,  winrate_pct: 0 },
    { key: "month", label: "本月", from: "2026-11-01", to: "2026-11-08", scope: "1 号至今", net_usd: -87.07, n_trades: 4,  wins: 0,  losses: 4,  winrate_pct: 0 },
    { key: "year",  label: "本年", from: "2026-01-01", to: "2026-11-08", scope: "年初至今", net_usd: -175.11, n_trades: 47, wins: 17, losses: 30, winrate_pct: 36.2 },
    { key: "total", label: "总计", from: "2026-10-02", to: "2026-11-06", scope: "全部历史", net_usd: -175.11, n_trades: 47, wins: 17, losses: 30, winrate_pct: 36.2 },
  ],
};

console.log("【1】正常渲染");
w.renderPnlPeriods(FILL, "pnl-periods");
const el = w.document.getElementById("pnl-periods");
check("卡片渲染出内容", el && el.innerHTML.length > 50);

const rows = el.querySelectorAll("tbody tr");
check("渲染出 4 行（周/月/年/总计）", rows.length === 4, `实际 ${rows.length}`);

const totalRow = rows[3];
check("总计行带 pp-total 标记", totalRow.className.indexOf("pp-total") > -1);
const totalCells = totalRow.querySelectorAll("td");
check("总计行净盈亏 = -175.11U（与胜率卡累计净盈亏同数）",
  totalCells[2].textContent.trim().indexOf("-175.11") > -1,
  `实际 ${totalCells[2].textContent.trim()}`);
check("总计行胜率 36.2%", totalCells[4].textContent.trim() === "36.2%",
  `实际 ${totalCells[4].textContent.trim()}`);
check("总计行笔数 47", totalCells[3].textContent.trim() === "47",
  `实际 ${totalCells[3].textContent.trim()}`);
check("总行红字（负数 → .neg）", totalCells[2].className.indexOf("neg") > -1,
  `实际 className=${totalCells[2].className}`);

const weekRow = rows[0];
check("本周行 label 正确", weekRow.querySelectorAll("td")[0].textContent.trim() === "本周");
check("本周行带区间文案「周一至今」",
  weekRow.querySelectorAll("td")[1].textContent.indexOf("周一至今") > -1);
check("本周行净盈亏 = -87.07U",
  weekRow.querySelectorAll("td")[2].textContent.trim().indexOf("-87.07") > -1,
  `实际 ${weekRow.querySelectorAll("td")[2].textContent.trim()}`);

console.log("\n【2】0 笔周期：显示「—」而不是 +0.00U（无样本 ≠ 0 结果）");
w.renderPnlPeriods({
  basis: "x", n_settled: 1, n_excluded: 0,
  periods: [
    { key: "week",  label: "本周", from: "2026-11-02", to: "2026-11-08", scope: "周一至今",  net_usd: 0, n_trades: 0, wins: 0, losses: 0, winrate_pct: null },
    { key: "month", label: "本月", from: "2026-11-01", to: "2026-11-08", scope: "1 号至今", net_usd: 0, n_trades: 0, wins: 0, losses: 0, winrate_pct: null },
    { key: "year",  label: "本年", from: "2026-01-01", to: "2026-11-08", scope: "年初至今", net_usd: 5, n_trades: 1, wins: 1, losses: 0, winrate_pct: 100 },
    { key: "total", label: "总计", from: "2026-11-02", to: "2026-11-02", scope: "全部历史", net_usd: 5, n_trades: 1, wins: 1, losses: 0, winrate_pct: 100 },
  ],
}, "pnl-periods");
const zrows = el.querySelectorAll("tbody tr");
check("0 笔行净盈亏显示「—」", zrows[0].querySelectorAll("td")[2].textContent.trim() === "—",
  `实际 ${zrows[0].querySelectorAll("td")[2].textContent.trim()}`);
check("0 笔行胜率显示「—」（不是 0%）", zrows[0].querySelectorAll("td")[4].textContent.trim() === "—",
  `实际 ${zrows[0].querySelectorAll("td")[4].textContent.trim()}`);
check("0 笔行不带 pos/neg 染色（无样本不判输赢）",
  /pos|neg/.test(zrows[0].querySelectorAll("td")[2].className) === false,
  `实际 className=${zrows[0].querySelectorAll("td")[2].className}`);
check("有笔的行仍染色", /pos/.test(zrows[2].querySelectorAll("td")[2].className));

console.log("\n【3】指标全中文（不出现 WR/NET/PF/ES 等英文缩写）");
const text = el.textContent;
check("无「WR」", text.indexOf("WR") === -1);
check("无「NET」", text.indexOf("NET") === -1);
check("无「PF」", text.indexOf("PF") === -1);
check("表头是中文", /净盈亏/.test(text) && /笔数/.test(text) && /胜率/.test(text));
check("表头含「周期」「区间」", /周期/.test(text) && /区间/.test(text));

console.log("\n【4】健壮性");
w.renderPnlPeriods(null, "pnl-periods");
check("null 输入不抛错且有内容", el.innerHTML.length > 10);
w.renderPnlPeriods({ error: "journal.jsonl 不存在" }, "pnl-periods");
check("error 回落显示错误文案", el.textContent.indexOf("journal.jsonl 不存在") > -1);
w.renderPnlPeriods({ basis: "x", periods: [] }, "pnl-periods");
check("空 periods 显示「暂无收益数据」", el.textContent.indexOf("暂无收益数据") > -1);

console.log("\n【5】训练环境同款（#train-pnl-periods）");
w.renderPnlPeriods(FILL, "train-pnl-periods");
const tel = w.document.getElementById("train-pnl-periods");
check("训练卡也有 4 行", tel.querySelectorAll("tbody tr").length === 4);
check("训练卡总计同数", tel.querySelectorAll("tbody tr")[3].querySelectorAll("td")[2].textContent.trim()
  .indexOf("-175.11") > -1);

console.log(`\n═══ 结果：${pass} 通过 / ${fail} 失败 ═══`);
process.exit(fail > 0 ? 1 : 0);
