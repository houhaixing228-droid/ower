/* 经营看板前端。原生 JS + 内联 SVG，不引任何 CDN：评审环境可能是干净的离线机器。 */

const state = {
  period: { start: "2026-05-01", end: "2026-08-31" },
  sessionId: null,
};

const $ = (sel) => document.querySelector(sel);
const esc = (text) => String(text ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (v) => (v === null || v === undefined)
  ? "—"
  : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const int = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("zh-CN");

async function api(path, options) {
  const res = await fetch(path, options);
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch (err) {
    throw new Error(path + " 返回的不是 JSON：" + text.slice(0, 120));
  }
}

// -- 启动 -------------------------------------------------------------------

async function boot() {
  const health = await api("/api/health");
  const catalog = await api("/api/catalog");
  state.period = catalog.data_period || health.data_period || state.period;

  $("#badge-mode").textContent = "模型：" + (health.llm_mode === "live" ? "已接入（live）" : "降级（mock）");
  $("#badge-docs").textContent = "知识库：" + health.kb_docs + " 份 / " + health.kb_chunks + " 片段";
  $("#badge-rows").textContent = "有效明细：" + int(health.valid_sales_rows) + " 行";
  $("#period-line").textContent = "数据区间 " + state.period.start + " 至 " + state.period.end
    + " · 系统今天 2026-09-01";
  $("#foot-note").textContent = "口径以知识库 KB-001（指标口径手册 v3）为准；"
    + "经营数字全部来自清洗后的销售明细，文档里的估算值不作答案。";

  const select = $("#store");
  catalog.stores.forEach((store) => {
    const option = document.createElement("option");
    option.value = store.store_id;
    option.textContent = store.store_id + " " + store.store_name;
    select.appendChild(option);
  });

  $("#start").value = state.period.start;
  $("#end").value = state.period.end;
  newSession();
  await loadAll();
  renderQuality();
}

function newSession() {
  state.sessionId = "web-" + Math.random().toString(36).slice(2, 10);
  $("#session-tag").textContent = "session：" + state.sessionId;
  $("#chat").innerHTML = "";
}

// -- 指标 -------------------------------------------------------------------

async function loadAll() {
  const params = new URLSearchParams({
    start: $("#start").value,
    end: $("#end").value,
  });
  const store = $("#store").value;
  if (store) params.set("store_id", store);

  await Promise.all([
    loadSummary(params),
    loadDaily(params),
    loadTop(params),
    loadPayments(params),
  ]);
}

async function loadSummary(params) {
  const data = await api("/api/metrics/summary?" + params.toString());
  $("#kpis").innerHTML = [
    ["净营业额", "¥" + money(data.net_revenue), "销售行 − 退款行"],
    ["退款金额", "¥" + money(data.refund_amount), "退款行绝对值"],
    ["有效订单数", int(data.orders), "不同订单号，多行订单算 1 单"],
    ["客单价", data.aov === null ? "—" : "¥" + money(data.aov), "净营业额 ÷ 有效订单数"],
    ["销量", int(data.qty), "销售数量 − 退款数量"],
  ].map(([k, v, note]) =>
    `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="note">${note}</div></div>`
  ).join("");
}

async function loadDaily(params) {
  const data = await api("/api/metrics/daily?" + params.toString());
  renderChart(data.days || []);
}

async function loadTop(params) {
  const query = new URLSearchParams(params);
  query.set("limit", "10");
  const data = await api("/api/top_products?" + query.toString());
  const rows = (data.products || []).map((item, index) =>
    `<tr><td>${index + 1}</td><td>${esc(item.product_name || item.product_id)}</td>` +
    `<td class="num">${int(item.qty)}</td><td class="num">¥${money(item.net_revenue)}</td>` +
    `<td class="num">${int(item.orders)}</td></tr>`
  ).join("");
  $("#top-table").innerHTML =
    "<thead><tr><th>#</th><th>商品</th><th class='num'>销量</th><th class='num'>净营业额</th>" +
    "<th class='num'>订单数</th></tr></thead><tbody>" +
    (rows || "<tr><td colspan='5'>这段时间没有数据</td></tr>") + "</tbody>";
}

async function loadPayments(params) {
  const data = await api("/api/payment_mix?" + params.toString());
  const payments = Object.entries(data.payments || {});
  const max = Math.max(1, ...payments.map(([, v]) => v.net_revenue || 0));
  $("#pay-mix").innerHTML = payments.map(([name, value]) => `
    <div class="row">
      <div class="name">${esc(name)}</div>
      <div class="bar"><div class="fill" style="width:${((value.net_revenue || 0) / max * 100).toFixed(1)}%"></div></div>
      <div class="val">¥${money(value.net_revenue)}</div>
    </div>`).join("") || "<p class='hint'>这段时间没有数据</p>";
}

// -- 趋势图（内联 SVG，不依赖任何图表库） -------------------------------------

function renderChart(days) {
  const host = $("#chart");
  if (!days.length) {
    host.innerHTML = "<p class='hint'>这段时间没有数据</p>";
    return;
  }
  const width = Math.max(560, days.length * 18);
  const height = 240;
  const pad = { l: 56, r: 48, t: 16, b: 34 };
  const innerW = width - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;

  const maxRevenue = Math.max(1, ...days.map((d) => d.net_revenue));
  const maxOrders = Math.max(1, ...days.map((d) => d.orders));
  const x = (i) => pad.l + (days.length === 1 ? innerW / 2 : (innerW * i) / (days.length - 1));
  const yR = (v) => pad.t + innerH - (v / maxRevenue) * innerH;
  const yO = (v) => pad.t + innerH - (v / maxOrders) * (innerH * 0.35);

  const barW = Math.max(2, innerW / days.length * 0.5);
  const bars = days.map((d, i) =>
    `<rect x="${(x(i) - barW / 2).toFixed(1)}" y="${yO(d.orders).toFixed(1)}" width="${barW.toFixed(1)}"
      height="${(pad.t + innerH - yO(d.orders)).toFixed(1)}" fill="#cfd9e6" rx="1"><title>${d.date} 订单 ${d.orders}</title></rect>`
  ).join("");

  const line = days.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${yR(d.net_revenue).toFixed(1)}`).join(" ");
  const area = `${line} L${x(days.length - 1).toFixed(1)},${pad.t + innerH} L${x(0).toFixed(1)},${pad.t + innerH} Z`;
  const dots = days.map((d, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${yR(d.net_revenue).toFixed(1)}" r="2.4" fill="#1f6feb"><title>${d.date} ¥${d.net_revenue.toFixed(2)}</title></circle>`
  ).join("");

  const step = Math.max(1, Math.ceil(days.length / 8));
  const ticks = days.map((d, i) => i % step === 0
    ? `<text x="${x(i).toFixed(1)}" y="${height - 12}" font-size="10" fill="#5b6878" text-anchor="middle">${d.date.slice(5)}</text>`
    : "").join("");
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((r) => {
    const value = maxRevenue * r;
    return `<text x="${pad.l - 8}" y="${(yR(value) + 3).toFixed(1)}" font-size="10" fill="#5b6878" text-anchor="end">${Math.round(value)}</text>` +
      `<line x1="${pad.l}" x2="${width - pad.r}" y1="${yR(value).toFixed(1)}" y2="${yR(value).toFixed(1)}" stroke="#eef1f5"/>`;
  }).join("");

  host.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
    ${yTicks}${bars}
    <path d="${area}" fill="#1f6feb" opacity="0.08"/>
    <path d="${line}" fill="none" stroke="#1f6feb" stroke-width="2"/>
    ${dots}${ticks}
    <text x="${width - pad.r + 6}" y="${pad.t + 10}" font-size="10" fill="#5b6878">订单</text>
  </svg>`;
}

// -- 数据质量 ---------------------------------------------------------------

async function renderQuality() {
  const data = await api("/api/data_quality");
  const report = data.cleaning_report || {};
  const removed = report.removed || {};
  const labels = {
    "1_unparseable_date": "日期解析不出来",
    "2_empty_amount": "金额为空（不回填）",
    "3_qty_le_zero": "数量 ≤ 0",
    "4_store_not_in_stores": "门店不在维表",
    "5_product_not_in_products": "商品不在维表",
    "6_duplicate_row": "完全重复的明细行",
  };
  const total = Object.values(removed).reduce((a, b) => a + (b || 0), 0) || 1;
  const rows = Object.keys(labels).map((key) => {
    const value = removed[key] || 0;
    return `<div class="row"><div>${labels[key]}</div>
      <div class="bar"><div class="fill" style="width:${(value / total * 100).toFixed(1)}%"></div></div>
      <div class="val">${value}</div></div>`;
  }).join("");

  $("#quality").innerHTML = `<div class="quality">
    <div class="summary">原始 ${int(report.raw_rows)} 行 → 保留 <b>${int(report.kept_rows)}</b> 行
      （销售 ${int(report.kept_sales_rows)} 行 + 退款 ${int(report.kept_refund_rows)} 行），
      共剔除 ${int(total - (removed.note_unparseable_amount || 0))} 行。</div>
    ${rows}
    <div class="warn">${(data.kb_warnings || []).map(esc).join("<br>")}</div>
  </div>`;
}

// -- 问答 -------------------------------------------------------------------

function pushMessage(role, html) {
  const wrap = document.createElement("div");
  wrap.className = "msg" + (role === "me" ? " me" : "");
  wrap.innerHTML = html;
  $("#chat").appendChild(wrap);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return wrap;
}

function renderAnswer(payload, node) {
  const type = payload.answer_type || "data";
  const cites = (payload.citations || []).map((c) =>
    `<div class="cite"><div class="doc">${esc(c.doc_id)}</div><div class="quote">${esc(c.quote)}</div></div>`
  ).join("");
  const evid = (payload.data_evidence || []).length
    ? `<details class="evid"><summary>数据证据 ${payload.data_evidence.length} 条（每条都记着用的参数和查到的数）</summary>
        ${payload.data_evidence.map((e) => `<pre>${esc(JSON.stringify(e, null, 1))}</pre>`).join("")}</details>`
    : "";
  node.innerHTML = `<div class="bubble">${esc(payload.answer || "")}</div>
    <div class="meta"><span class="tag ${type}">${type}</span>
      <button class="ghost" data-trace="${esc(payload.trace_id)}">看 trace</button></div>
    ${cites}${evid}`;
  node.querySelector("[data-trace]").addEventListener("click", () => loadTrace(payload.trace_id));
}

async function ask(question) {
  pushMessage("me", `<div class="who">运营</div><div class="bubble">${esc(question)}</div>`);
  const node = pushMessage("bot", `<div class="who">助手</div><div class="bubble">查数、检索、组织回答中…</div>`);
  try {
    const payload = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, question }),
    });
    renderAnswer(payload, node);
  } catch (err) {
    node.innerHTML = `<div class="who">助手</div><div class="bubble">请求失败：${esc(err.message)}</div>`;
  }
}

// -- trace 面板（第四关） ----------------------------------------------------

async function loadTrace(traceId) {
  $("#trace-tag").textContent = traceId;
  const host = $("#trace");
  host.innerHTML = "<p class='empty'>加载中…</p>";
  let trace;
  try {
    trace = await api("/api/trace/" + encodeURIComponent(traceId));
  } catch (err) {
    host.innerHTML = "<p class='empty'>" + esc(err.message) + "</p>";
    return;
  }

  const steps = (trace.steps || []).map((step) => {
    const detail = step.detail || {};
    let text = "";
    if (step.step === "plan") {
      text = `意图 ${detail.intent}/${detail.kind}｜窗口 ${(detail.window || []).join(" ~ ")}｜检索句 ${detail.search_query || ""}`;
    } else if (step.step === "tool") {
      text = `${detail.tool} ${JSON.stringify(detail.params || {})}`;
    } else if (step.step === "search") {
      text = `命中 ${(detail.hits || []).length} 条，覆盖率 ${detail.coverage}`;
    } else {
      text = JSON.stringify(detail).slice(0, 300);
    }
    return `<div class="step"><span class="nm">${esc(step.step)}</span>
      <span class="ms">${step.took_ms === null ? "" : step.took_ms + " ms"}</span>
      <span class="dt">${esc(text)}</span></div>`;
  }).join("");

  const hits = [];
  const filtered = [];
  (trace.steps || []).forEach((step) => {
    const detail = step.detail || {};
    if (step.step === "search") {
      (detail.hits || []).forEach((hit) => hits.push(hit));
      (detail.filtered || []).forEach((item) => filtered.push(item));
    }
  });

  const calls = (trace.llm_calls || []).map((call, index) => `
    <h3>第 ${index + 1} 次调用模型 · ${esc(call.model)} · ${call.status} · finish=${esc(call.finish_reason)} · ${call.took_ms} ms</h3>
    <pre>${esc(call.prompt || "")}</pre>
    <h4>模型原始输出</h4>
    <pre>${esc(call.raw_content || (call.tool_calls || []).join(", "))}</pre>
    ${call.raw_reasoning ? "<h4>思考过程（不展示给用户）</h4><pre>" + esc(call.raw_reasoning) + "</pre>" : ""}
  `).join("");

  host.innerHTML = `
    <div class="step"><span class="nm">trace_id</span><span class="dt">${esc(trace.trace_id)}</span></div>
    <div class="step"><span class="nm">问题</span><span class="dt">${esc(trace.question)}</span></div>
    <div class="step"><span class="nm">总耗时</span><span class="dt">${trace.total_ms} ms</span></div>
    <h3>每一步</h3>${steps || "<p class='empty'>没有步骤</p>"}
    <h3>检索到的片段（含分数）</h3>
    ${hits.map((hit) => `<div class="hit"><span>${esc(hit.doc_id)} ${esc(hit.chunk_id)}</span>
      <span>${hit.padded ? "<span class='padded'>补位</span> " : ""}${esc((hit.dropped_instructions || []).join("；"))}</span>
      <span class="sc">${hit.score}</span></div>`).join("") || "<p class='empty'>这一轮没有检索</p>"}
    <h3>被过滤掉的文档</h3>
    ${filtered.map((item) => `<div class="step"><span class="nm">${esc(item.doc_id)}</span>
      <span class="dt">${esc(item.reason)}</span></div>`).join("") || "<p class='empty'>没有</p>"}
    <h3>发给模型的请求</h3>${calls || "<p class='empty'>没有调用模型（降级模式或已拒答）</p>"}
    ${trace.errors && trace.errors.length ? "<h3>错误</h3><pre>" + esc(JSON.stringify(trace.errors, null, 1)) + "</pre>" : ""}
  `;
}

// -- 事件 -------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-load").addEventListener("click", loadAll);
  $("#btn-new-session").addEventListener("click", newSession);
  $("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#question");
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    ask(question);
  });
  document.querySelectorAll("[data-quick]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.quick;
      if (kind === "all") { $("#start").value = state.period.start; $("#end").value = state.period.end; }
      else {
        const month = kind.slice(1);
        $("#start").value = "2026-0" + month + "-01";
        $("#end").value = "2026-0" + month + "-" + (month === "6" ? "30" : "31");
      }
      loadAll();
    });
  });
  document.querySelectorAll(".samples .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $("#question").value = chip.textContent.trim();
      $("#question").focus();
    });
  });
  boot().catch((err) => {
    document.body.insertAdjacentHTML("afterbegin",
      "<pre style='margin:16px;color:#c0392b'>初始化失败：" + esc(err.message) + "</pre>");
  });
});
