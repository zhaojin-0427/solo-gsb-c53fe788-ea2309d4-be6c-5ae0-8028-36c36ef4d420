"use strict";

const $ = (sel) => document.querySelector(sel);
const tbodyPoints = $("#pointsTable tbody");
const errBox = $("#diagError");
const verdictClass = {
  healthy: "same",
  single: "up",
  ambiguous: "",
  outside_model: "",
};

let records = [];
let selectedRecord = null;
let numPoints = 3;

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmt = (x, d = 4) =>
  (x === null || x === undefined) ? "—"
  : Number(x).toFixed(d).replace(/\.?0+$/, "");

/* ---------------- 记录选择 ---------------- */

async function loadRecords(preselectId) {
  const res = await fetch("/api/trials");
  const data = await res.json();
  records = data.trials;
  const sel = $("#recordSelect");
  sel.innerHTML = "";
  if (!records.length) {
    sel.innerHTML = '<option value="">（暂无历史试算记录，请先到主页试算并保存）</option>';
    $("#diagnoseBtn").disabled = true;
    return;
  }
  $("#diagnoseBtn").disabled = false;
  records.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `#${t.id} ${t.name}（${t.num_groups} 组 / ${t.num_baseline_strings} 串，${t.created_at.replace("T", " ").replace("+00:00", "")}）`;
    sel.appendChild(opt);
  });
  if (preselectId && records.some((t) => t.id === preselectId)) {
    sel.value = preselectId;
  }
  onSelectRecord();
}

async function onSelectRecord() {
  const id = Number($("#recordSelect").value);
  selectedRecord = null;
  const info = $("#recordInfo");
  if (!id) { info.style.display = "none"; return; }
  const res = await fetch(`/api/trials/${id}`);
  if (!res.ok) { info.style.display = "none"; return; }
  selectedRecord = await res.json();
  const base = selectedRecord.result.baseline;
  info.style.display = "block";
  const wiring = base.strings.map((s) => "[" + s.map(escapeHtml).join("—") + "]").join(" ∥ ");
  info.innerHTML =
    `记录 <b>#${selectedRecord.id}</b>（${escapeHtml(selectedRecord.name)}）` +
    `基线接线：${wiring}；基线开路电压（电压上限）= <b>${fmt(base.voc)}</b> V，` +
    `零电压理论电流 = <b>${fmt(base.i_at_zero)}</b> A。`;
}

/* ---------------- 测点表 ---------------- */

function pointRows() {
  return [...tbodyPoints.querySelectorAll("tr")].map((tr) => ({
    v: tr.querySelector(".p-v"),
    i: tr.querySelector(".p-i"),
  }));
}

function renderPointRows() {
  const prev = pointRows().map((r) => ({ v: r.v.value, i: r.i.value }));
  tbodyPoints.innerHTML = "";
  for (let i = 0; i < numPoints; i++) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${i + 1}</td>` +
      `<td><input class="p-v" type="number" min="0" step="any" placeholder="有限非负数"></td>` +
      `<td><input class="p-i" type="number" min="0" step="any" placeholder="有限非负数"></td>`;
    tbodyPoints.appendChild(tr);
  }
  pointRows().forEach((r, i) => {
    if (prev[i]) { r.v.value = prev[i].v; r.i.value = prev[i].i; }
  });
}

function showError(msg) {
  errBox.textContent = "✗ " + msg;
  errBox.hidden = false;
}
function clearError() { errBox.hidden = true; }

/* ---------------- 提交诊断 ---------------- */

async function submitDiagnosis() {
  clearError();
  $("#diagResultCard").hidden = true;
  if (!selectedRecord) { showError("请先选择一条历史试算记录"); return; }

  const tolRaw = $("#tolerance").value;
  const tolerance = Number(tolRaw);
  if (tolRaw.trim() === "" || !Number.isFinite(tolerance) || tolerance < 0) {
    showError("电流容差必须是有限非负数"); return;
  }

  const points = [];
  const seen = new Set();
  for (const [idx, r] of pointRows().entries()) {
    const v = Number(r.v.value), i = Number(r.i.value);
    if (r.v.value.trim() === "" || r.i.value.trim() === ""
        || !Number.isFinite(v) || !Number.isFinite(i) || v < 0 || i < 0) {
      showError(`第 ${idx + 1} 个测点非法：V、I 都必须是有限非负数`); return;
    }
    const key = String(v);
    if (seen.has(key)) { showError(`第 ${idx + 1} 个测点电压与前面重复，各点电压必须互异`); return; }
    seen.add(key);
    points.push({ v, i });
  }
  if (points.length < 3) { showError("至少提交 3 个电压互异的实测点"); return; }

  try {
    const res = await fetch(
      `/api/trials/${selectedRecord.id}/diagnose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tolerance, points }),
      });
    const data = await res.json();
    if (!res.ok) { showError(data.detail || "诊断请求失败"); return; }
    renderDiagnosis(data);
  } catch (e) {
    showError("网络/服务错误：" + e.message);
  }
}

function renderDiagnosis(d) {
  $("#diagResultCard").hidden = false;
  const box = $("#verdictBox");
  box.className = "gain " + (verdictClass[d.verdict] || "");
  const label = {
    healthy: "无法确认异常",
    single: "定位到单组 Isc 下降",
    ambiguous: "多组歧义",
    outside_model: "模型外偏差",
  }[d.verdict];
  box.innerHTML = `<b>结论（${label}）：</b>${escapeHtml(d.message)}`;

  const tb = $("#diagPointsTable tbody");
  tb.innerHTML = "";
  d.points.forEach((p, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${i + 1}</td>` +
      `<td class="num">${fmt(p.v)}</td>` +
      `<td class="num">${fmt(p.i)}</td>` +
      `<td class="num">${fmt(p.theoretical_i)}</td>` +
      `<td class="num">${fmt(p.deviation)}</td>` +
      `<td>${p.within_tolerance ? "✓" : "✗"}</td>`;
    tb.appendChild(tr);
  });

  const tg = $("#diagGroupsTable tbody");
  tg.innerHTML = "";
  d.groups.forEach((g) => {
    const tr = document.createElement("tr");
    const range = g.explainable
      ? `${fmt(g.isc_min, 2)} ～ ${fmt(g.isc_max, 2)}`
      : "—";
    tr.innerHTML =
      `<td><b>${escapeHtml(g.group_id)}</b></td>` +
      `<td class="num">${fmt(g.isc_original, 2)}</td>` +
      `<td>${g.explainable ? "✓ 可解释" : "✗"}</td>` +
      `<td class="num">${range}</td>`;
    tg.appendChild(tr);
  });
  $("#diagResultCard").scrollIntoView?.({ behavior: "smooth" });
}

/* ---------------- 事件绑定 ---------------- */

$("#recordSelect").addEventListener("change", onSelectRecord);
$("#addPointBtn").addEventListener("click", () => { numPoints++; renderPointRows(); });
$("#removePointBtn").addEventListener("click", () => {
  if (numPoints <= 3) return;
  numPoints--; renderPointRows();
});
$("#threePointsBtn").addEventListener("click", () => { numPoints = 3; renderPointRows(); });
$("#diagnoseBtn").addEventListener("click", submitDiagnosis);

/* ---------------- 初始化 ---------------- */

const preset = new URLSearchParams(location.search).get("t");
renderPointRows();
loadRecords(preset ? Number(preset) : null);
