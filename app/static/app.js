"use strict";

const MIN_G = 3, MAX_G = 9, MAX_S = 3;

const $ = (sel) => document.querySelector(sel);
const tbodyGroups = $("#groupsTable tbody");
const baselineBox = $("#baselineStrings");
const errBox = $("#errorBox"), okBox = $("#okBox");

let numGroups = MIN_G;
let numStrings = 1;
let currentResult = null;   // 最近一次试算结果
let currentPayload = null;

/* ---------------- 组录入 ---------------- */

function groupInputs() {
  return [...tbodyGroups.querySelectorAll("tr")].map((tr) => ({
    id: tr.querySelector(".g-id"),
    voc: tr.querySelector(".g-voc"),
    isc: tr.querySelector(".g-isc"),
  }));
}

function renderGroupRows() {
  const prev = collectGroupsRaw();
  tbodyGroups.innerHTML = "";
  for (let i = 0; i < numGroups; i++) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input class="g-id" type="text" maxlength="32" placeholder="如 M${i + 1}"></td>` +
      `<td><input class="g-voc" type="text" inputmode="decimal" placeholder="0.01～100"></td>` +
      `<td><input class="g-isc" type="text" inputmode="decimal" placeholder="0.01～30"></td>`;
    tbodyGroups.appendChild(tr);
  }
  const rows = groupInputs();
  prev.forEach((p, i) => {
    if (!rows[i]) return;
    rows[i].id.value = p.id; rows[i].voc.value = p.voc; rows[i].isc.value = p.isc;
  });
  rows.forEach((r) => Object.values(r).forEach((el) =>
    el.addEventListener("input", renderBaselineChips)));
  renderBaselineChips();
}

function collectGroupsRaw() {
  return groupInputs().map((r) => ({
    id: r.id.value.trim(), voc: r.voc.value.trim(), isc: r.isc.value.trim(),
  }));
}

/* ---------------- 基线接线（芯片点选） ---------------- */

function renderBaselineChips(pendingAssign) {
  const groups = collectGroupsRaw();
  const validIds = groups.map((g) => g.id).filter(Boolean);
  // 保留旧分配；pendingAssign 用于表单回填
  const prev = pendingAssign || baselineAssignments();
  const usedElsewhere = (sIdx, gid) =>
    prev.some((ids, j) => j !== sIdx && ids.includes(gid));

  baselineBox.innerHTML = "";
  for (let s = 0; s < numStrings; s++) {
    const row = document.createElement("div");
    row.className = "string-row";
    row.innerHTML = `<span class="tag">串 ${s + 1}</span><div class="chip-select"></div>`;
    const select = row.querySelector(".chip-select");
    validIds.forEach((gid) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = gid;
      if (prev[s] && prev[s].includes(gid)) chip.classList.add("on");
      if (usedElsewhere(s, gid)) chip.classList.add("disabled");
      chip.addEventListener("click", () => {
        const cur = baselineAssignments();
        if (chip.classList.contains("on")) {
          // 在本串上再点一次 = 移出本串
          cur[s] = cur[s].filter((x) => x !== gid);
        } else {
          // 在其他串点亮 = 从原串移到本串（每组全局至多出现一次）
          cur[s] = [...cur[s], gid];
          cur.forEach((ids, j) => {
            if (j !== s) cur[j] = ids.filter((x) => x !== gid);
          });
        }
        renderBaselineChips(cur);
      });
      select.appendChild(chip);
    });
    if (!validIds.length) select.innerHTML =
      '<span style="color:#94a3b8;font-size:12px">请先填写组编号</span>';
    baselineBox.appendChild(row);
  }
}

function baselineAssignments() {
  return [...baselineBox.querySelectorAll(".string-row")].map((row) =>
    [...row.querySelectorAll(".chip.on")].map((c) => c.textContent));
}

/* ---------------- 提交 ---------------- */

function gatherPayload() {
  const groups = collectGroupsRaw();
  const groupsOut = groups.map((g) => ({ id: g.id, voc: g.voc, isc: g.isc }));
  const baseline = baselineAssignments();
  return {
    name: $("#trialName").value.trim(),
    groups: groupsOut,
    baseline,
  };
}

function showError(msg) {
  errBox.textContent = "✗ " + msg;
  errBox.hidden = false;
  okBox.hidden = true;
}
function clearMsg() { errBox.hidden = true; okBox.hidden = true; }

async function solve(reopenPayload = null) {
  clearMsg();
  let payload;
  try {
    payload = reopenPayload || gatherPayload();
  } catch (e) { showError(e.message); return; }

  try {
    const res = await fetch("/api/trials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) { showError(data.detail || "请求失败"); return; }
    currentResult = data.result;
    currentPayload = payload;
    okBox.textContent = `✓ 已保存为记录 #${data.id}`;
    okBox.hidden = false;
    renderResult(data.result);
    loadHistory();
  } catch (e) {
    showError("网络/服务错误：" + e.message);
  }
}

/* Reopen 时不再重复写库：直接本地复算不合适（模型在后端），
   因此重开走 GET 展示；若用户改动后再点试算才生成新记录。 */
function displayReopened(record) {
  clearMsg();
  fillForm(record.payload);
  currentResult = record.result;
  currentPayload = record.payload;
  renderResult(record.result);
  okBox.textContent = `✓ 已重开记录 #${record.id}（${record.name}）；改动后点“开始试算”另存新记录`;
  okBox.hidden = false;
  $("#resultCard").scrollIntoView?.({ behavior: "smooth" });
}

function fillForm(payload) {
  numGroups = Math.min(MAX_G, Math.max(MIN_G, payload.groups.length));
  renderGroupRows();
  const rows = groupInputs();
  payload.groups.forEach((g, i) => {
    rows[i].id.value = g.id;
    rows[i].voc.value = g.voc;
    rows[i].isc.value = g.isc;
  });
  $("#trialName").value = payload.name || "";
  numStrings = Math.min(MAX_S, Math.max(1, payload.baseline.length));
  renderBaselineChips(payload.baseline.map((s) => [...s]));
}

/* ---------------- 结果渲染 ---------------- */

const fmt = (x, d = 3) =>
  (x === null || x === undefined) ? "—" : Number(x).toFixed(d).replace(/\.?0+$/, "");

function renderWiringInto(el, w) {
  el.innerHTML = "";
  w.strings.forEach((s, idx) => {
    const div = document.createElement("div");
    div.className = "wstring";
    div.innerHTML = `<span class="par">${idx === 0 ? "⎓⎓ 并联支路" : ""}</span>` +
      s.map((g) => `<b>${escapeHtml(g)}</b>`).join('<span class="plus">—</span>');
    el.appendChild(div);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderResult(r) {
  $("#resultCard").hidden = false;
  renderWiringInto($("#baselineWiring"), r.baseline);
  renderWiringInto($("#optimalWiring"), r.optimal);

  $("#baseVp").textContent = fmt(r.baseline.peak.v);
  $("#baseIp").textContent = fmt(r.baseline.peak.i);
  $("#basePp").textContent = fmt(r.baseline.peak.p);
  $("#baseVoc").textContent = fmt(r.baseline.voc);
  $("#optVp").textContent = fmt(r.optimal.peak.v);
  $("#optIp").textContent = fmt(r.optimal.peak.i);
  $("#optPp").textContent = fmt(r.optimal.peak.p);
  $("#optVoc").textContent = fmt(r.optimal.voc);

  const badge = $("#optBadge");
  const gain = $("#gainBox");
  if (r.comparison.same_wiring) {
    badge.hidden = true;
    gain.className = "gain same";
    gain.textContent = "基线接线即全局最优，重接线无额外功率收益。";
  } else {
    badge.hidden = false;
    badge.textContent = `${r.optimal.num_strings} 串`;
    gain.className = "gain up";
    const pct = r.comparison.gain_power_pct;
    gain.textContent = `重接线收益：峰值功率 +${fmt(r.comparison.gain_power)} W` +
      (pct > 0 ? `（+${fmt(pct, 2)}%）` : "") +
      `；峰值电压移动 ${r.comparison.gain_voltage >= 0 ? "+" : ""}` +
      `${fmt(r.comparison.gain_voltage)} V。`;
  }

  drawChart($("#pvChart"), [
    { curve: r.baseline.curve, color: "#d97742", y: "p", mpp: r.baseline.peak },
    { curve: r.optimal.curve, color: "#1b9e6b", y: "p", mpp: r.optimal.peak },
  ], "端电压 V (V)", "功率 P (W)");

  drawChart($("#ivChart"), [
    { curve: r.baseline.curve, color: "#d97742", y: "i", mpp: r.baseline.peak },
    { curve: r.optimal.curve, color: "#1b9e6b", y: "i", mpp: r.optimal.peak },
  ], "端电压 V (V)", "电流 I (A)");

  renderCandidates(r.candidates);
}

function renderCandidates(cands) {
  $("#candCount").textContent = cands.length;
  const tb = $("#candidatesTable tbody");
  tb.innerHTML = "";
  cands.forEach((c, i) => {
    const tr = document.createElement("tr");
    const wiring = c.strings.map((s) => "[" + s.join("—") + "]").join(" ∥ ");
    tr.innerHTML = `<td class="num">${i + 1}</td>` +
      `<td class="num">${c.num_strings}</td>` +
      `<td>${escapeHtml(wiring)}</td>` +
      `<td class="num">${fmt(c.peak_voltage)}</td>` +
      `<td class="num">${fmt(c.peak_power)}</td>`;
    tb.appendChild(tr);
  });
}

/* ---------------- Canvas 绘图 ---------------- */

function niceMax(x) {
  if (x <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const n = x / p;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return m * p;
}

function drawChart(canvas, series, xLabel, yLabel) {
  const dpr = window.devicePixelRatio || 1;
  const cw = canvas.clientWidth || 900;
  const ch = canvas.id === "ivChart" ? 300 : 380;
  canvas.width = Math.round(cw * dpr);
  canvas.height = Math.round(ch * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cw, ch);

  const pad = { l: 64, r: 18, t: 16, b: 42 };
  let xMax = 0, yMax = 0;
  series.forEach((s) => {
    xMax = Math.max(xMax, ...s.curve.v);
    s.curve[s.y].forEach((v) => { yMax = Math.max(yMax, v); });
  });
  xMax = niceMax(xMax * 1.02);
  yMax = niceMax(yMax * 1.05);

  const X = (v) => pad.l + (v / xMax) * (cw - pad.l - pad.r);
  const Y = (p) => ch - pad.b - (p / yMax) * (ch - pad.t - pad.b);

  // 网格与刻度
  ctx.strokeStyle = "#e8edf3"; ctx.fillStyle = "#64748b";
  ctx.font = "11px sans-serif"; ctx.lineWidth = 1;
  const NX = 8, NY = 5;
  for (let i = 0; i <= NX; i++) {
    const x = pad.l + i * (cw - pad.l - pad.r) / NX;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, ch - pad.b); ctx.stroke();
    ctx.textAlign = "center";
    ctx.fillText(fmt(xMax * i / NX, 2), x, ch - pad.b + 15);
  }
  ctx.textAlign = "right";
  for (let i = 0; i <= NY; i++) {
    const y = ch - pad.b - i * (ch - pad.t - pad.b) / NY;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(cw - pad.r, y); ctx.stroke();
    ctx.fillText(fmt(yMax * i / NY, 2), pad.l - 7, y + 4);
  }
  // 轴标题
  ctx.fillStyle = "#334155"; ctx.font = "12.5px sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(xLabel, (pad.l + cw - pad.r) / 2, ch - 6);
  ctx.save();
  ctx.translate(15, (pad.t + ch - pad.b) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(yLabel, 0, 0);
  ctx.restore();

  // 旁路拐点（在最优曲线上标记）
  const opt = series[series.length - 1].curve;
  ctx.fillStyle = "#94a3b8";
  opt.knees.forEach((v) => {
    const idx = nearestIndex(opt.v, v);
    const pv = opt[series[series.length - 1].y][idx];
    ctx.beginPath();
    ctx.arc(X(opt.v[idx]), Y(pv), 3, 0, Math.PI * 2);
    ctx.fill();
  });

  // 曲线
  series.forEach((s) => {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    s.curve.v.forEach((v, i) => {
      const x = X(v), y = Y(s.curve[s.y][i]);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    // MPP 标记
    ctx.fillStyle = s.color;
    ctx.beginPath();
    ctx.arc(X(s.mpp.v), Y(s.mpp[s.y]), 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke();
    // 竖直虚线到横轴
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = s.color;
    ctx.beginPath(); ctx.moveTo(X(s.mpp.v), Y(s.mpp[s.y]));
    ctx.lineTo(X(s.mpp.v), ch - pad.b); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = s.color;
    ctx.font = "bold 12px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(`(${fmt(s.mpp.v)}, ${fmt(s.mpp[s.y])})`,
      X(s.mpp.v) + 7, Y(s.mpp[s.y]) - 7);
  });
}

function nearestIndex(arr, v) {
  let best = 0, bd = Infinity;
  arr.forEach((x, i) => { const d = Math.abs(x - v); if (d < bd) { bd = d; best = i; } });
  return best;
}

/* ---------------- 历史记录 ---------------- */

async function loadHistory() {
  const res = await fetch("/api/trials");
  const data = await res.json();
  const tb = $("#historyTable tbody");
  tb.innerHTML = "";
  if (!data.trials.length) {
    tb.innerHTML = '<tr><td colspan="6" style="color:#94a3b8">暂无记录</td></tr>';
    return;
  }
  data.trials.forEach((t) => {
    const tr = document.createElement("tr");
    tr.className = "open-row";
    tr.innerHTML =
      `<td class="num">${t.id}</td><td>${escapeHtml(t.name)}</td>` +
      `<td class="num">${t.num_groups}</td><td class="num">${t.num_baseline_strings}</td>` +
      `<td>${t.created_at.replace("T", " ").replace("+00:00", "")}</td>` +
      `<td class="actions"><a class="mini-btn" href="/diagnose.html?id=${t.id}">现场诊断</a>` +
      `<button class="danger">删除</button></td>`;
    tr.addEventListener("click", (e) => {
      if (e.target.classList.contains("danger")) return;
      if (e.target.closest(".mini-btn")) return;
      reopenTrial(t.id);
    });
    tr.querySelector(".danger").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteTrial(t.id);
    });
    tb.appendChild(tr);
  });
}

async function reopenTrial(id) {
  const res = await fetch(`/api/trials/${id}`);
  if (!res.ok) { showError("记录不存在或已删除"); loadHistory(); return; }
  displayReopened(await res.json());
}

async function deleteTrial(id) {
  if (!confirm(`确定删除记录 #${id}？`)) return;
  const res = await fetch(`/api/trials/${id}`, { method: "DELETE" });
  if (res.ok) { clearMsg(); loadHistory(); }
}

/* ---------------- 事件绑定 ---------------- */

$("#addGroupBtn").addEventListener("click", () => {
  if (numGroups >= MAX_G) return;
  numGroups++; renderGroupRows();
});
$("#removeGroupBtn").addEventListener("click", () => {
  if (numGroups <= MIN_G) return;
  numGroups--; renderGroupRows();
});
$("#addStringBtn").addEventListener("click", () => {
  if (numStrings >= MAX_S) return;
  numStrings++; renderBaselineChips();
});
$("#removeStringBtn").addEventListener("click", () => {
  if (numStrings <= 1) return;
  numStrings--; renderBaselineChips();
});
$("#allSeriesBtn").addEventListener("click", () => {
  numStrings = 1;
  const allIds = collectGroupsRaw().map((g) => g.id).filter(Boolean);
  renderBaselineChips([allIds]);
});
$("#exampleBtn").addEventListener("click", () => {
  const example = {
    name: "示例：4 组混接",
    groups: [
      { id: "M1", voc: "36.5", isc: "8.2" },
      { id: "M2", voc: "36.5", isc: "8.2" },
      { id: "M3", voc: "40.0", isc: "6.0" },
      { id: "M4", voc: "30.0", isc: "9.5" },
    ],
    baseline: [["M1", "M2", "M3", "M4"]],
  };
  fillForm(example);
  clearMsg();
});
$("#solveBtn").addEventListener("click", () => solve());
window.addEventListener("resize", () => {
  if (currentResult) renderResult(currentResult);
});

/* ---------------- 初始化 ---------------- */

renderGroupRows();
loadHistory();
