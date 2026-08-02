const state = {
  categories: [],
  editingId: null,
};

const el = (id) => document.getElementById(id);

const fmtMoney = (n) => Number(n).toLocaleString("th-TH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const CATEGORY_EMOJI = [
  [["เงินเดือน"], "💼"],
  [["โบนัส"], "🎁"],
  [["ขายของ"], "🛍️"],
  [["รายได้อื่น"], "✨"],
  [["อาหาร", "ข้าว", "กิน"], "🍜"],
  [["เดินทาง", "รถ", "น้ำมัน"], "🚗"],
  [["ที่พัก", "เช่า", "บ้าน"], "🏠"],
  [["ช้อปปิ้ง", "ของใช้"], "🛒"],
  [["บิล", "สาธารณูปโภค", "ไฟ", "น้ำ", "เน็ต"], "💡"],
  [["สุขภาพ", "หมอ", "ยา"], "💊"],
  [["บันเทิง", "หนัง", "เกม"], "🎬"],
  [["การศึกษา", "เรียน", "หนังสือ"], "📚"],
  [["เงินเก็บ", "ออมเงิน", "ออมทรัพย์"], "🐷"],
];

function categoryEmoji(name, type) {
  const found = CATEGORY_EMOJI.find(([keywords]) => keywords.some((k) => name.includes(k)));
  if (found) return found[1];
  return type === "income" ? "💵" : "🏷️";
}

function todayISO() {
  const d = new Date();
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const msg = (data && data.error) || `เกิดข้อผิดพลาด (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

// ---------- Categories ----------

async function loadCategories() {
  state.categories = await api("/api/categories");
  renderCategoryLists();
  renderCategorySelects();
}

function renderCategoryLists() {
  const incomeList = el("cat-list-income");
  const expenseList = el("cat-list-expense");
  incomeList.innerHTML = "";
  expenseList.innerHTML = "";

  state.categories.forEach((c) => {
    const li = document.createElement("li");
    const nameSpan = document.createElement("span");
    nameSpan.textContent = `${categoryEmoji(c.name, c.type)} ${c.name}`;
    const delBtn = document.createElement("button");
    delBtn.className = "danger";
    delBtn.textContent = "🗑️ ลบ";
    delBtn.onclick = () => deleteCategory(c.id);
    li.appendChild(nameSpan);
    li.appendChild(delBtn);
    (c.type === "income" ? incomeList : expenseList).appendChild(li);
  });
}

function renderCategorySelects() {
  const txType = el("tx-type").value;
  const txCategory = el("tx-category");
  const filterCategory = el("filter-category");
  const prevTxVal = txCategory.value;
  const prevFilterVal = filterCategory.value;

  txCategory.innerHTML = "";
  state.categories
    .filter((c) => c.type === txType)
    .forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = `${categoryEmoji(c.name, c.type)} ${c.name}`;
      txCategory.appendChild(opt);
    });
  if ([...txCategory.options].some((o) => o.value === prevTxVal)) {
    txCategory.value = prevTxVal;
  }

  filterCategory.innerHTML = '<option value="">ทั้งหมด</option>';
  state.categories.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = `${categoryEmoji(c.name, c.type)} ${c.name} (${c.type === "income" ? "รายรับ" : "รายจ่าย"})`;
    filterCategory.appendChild(opt);
  });
  filterCategory.value = prevFilterVal;
}

async function deleteCategory(id) {
  if (!confirm("ลบหมวดหมู่นี้หรือไม่?")) return;
  el("cat-error").classList.add("hidden");
  try {
    await api(`/api/categories/${id}`, { method: "DELETE" });
    await loadCategories();
  } catch (err) {
    el("cat-error").textContent = err.message;
    el("cat-error").classList.remove("hidden");
  }
}

el("cat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  el("cat-error").classList.add("hidden");
  const name = el("cat-name").value.trim();
  const type = el("cat-type").value;
  try {
    await api("/api/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, type }),
    });
    el("cat-name").value = "";
    await loadCategories();
  } catch (err) {
    el("cat-error").textContent = err.message;
    el("cat-error").classList.remove("hidden");
  }
});

el("tx-type").addEventListener("change", renderCategorySelects);

// ---------- Transactions ----------

function resetForm() {
  state.editingId = null;
  el("tx-id").value = "";
  el("tx-form").reset();
  el("tx-date").value = todayISO();
  renderCategorySelects();
  el("existing-slip").classList.add("hidden");
  el("remove-slip").checked = false;
  el("form-title").textContent = "✏️ เพิ่มรายการใหม่";
  el("submit-btn").textContent = "✅ บันทึก";
  el("cancel-edit-btn").classList.add("hidden");
  el("form-error").classList.add("hidden");
}

function startEdit(tx) {
  state.editingId = tx.id;
  el("tx-id").value = tx.id;
  el("tx-type").value = tx.type;
  renderCategorySelects();
  el("tx-date").value = tx.date;
  el("tx-amount").value = tx.amount;
  el("tx-category").value = tx.category_id;
  el("tx-note").value = tx.note || "";
  el("tx-slip").value = "";
  el("remove-slip").checked = false;

  if (tx.slip_url) {
    el("existing-slip").classList.remove("hidden");
    el("existing-slip-link").href = tx.slip_url;
  } else {
    el("existing-slip").classList.add("hidden");
  }

  el("form-title").textContent = "📝 แก้ไขรายการ";
  el("submit-btn").textContent = "✅ บันทึกการแก้ไข";
  el("cancel-edit-btn").classList.remove("hidden");
  el("form-error").classList.add("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

el("cancel-edit-btn").addEventListener("click", resetForm);

el("tx-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  el("form-error").classList.add("hidden");

  const fd = new FormData();
  fd.append("date", el("tx-date").value);
  fd.append("type", el("tx-type").value);
  fd.append("category_id", el("tx-category").value);
  fd.append("amount", el("tx-amount").value);
  fd.append("note", el("tx-note").value);
  const file = el("tx-slip").files[0];
  if (file) fd.append("slip", file);
  if (el("remove-slip").checked) fd.append("remove_slip", "1");

  try {
    if (state.editingId) {
      await api(`/api/transactions/${state.editingId}`, { method: "PUT", body: fd });
    } else {
      await api("/api/transactions", { method: "POST", body: fd });
    }
    resetForm();
    await Promise.all([loadTransactions(), loadSummary()]);
  } catch (err) {
    el("form-error").textContent = err.message;
    el("form-error").classList.remove("hidden");
  }
});

async function deleteTransaction(id) {
  if (!confirm("ลบรายการนี้หรือไม่?")) return;
  try {
    await api(`/api/transactions/${id}`, { method: "DELETE" });
    await Promise.all([loadTransactions(), loadSummary()]);
  } catch (err) {
    alert(err.message);
  }
}

function currentFilters() {
  const params = new URLSearchParams();
  const month = el("filter-month").value;
  const type = el("filter-type").value;
  const category = el("filter-category").value;
  if (month) params.set("month", month);
  if (type) params.set("type", type);
  if (category) params.set("category_id", category);
  return params;
}

async function loadTransactions() {
  const params = currentFilters();
  const rows = await api(`/api/transactions?${params.toString()}`);
  renderTransactions(rows);
}

function renderTransactions(rows) {
  const tbody = el("tx-table-body");
  tbody.innerHTML = "";
  el("empty-msg").classList.toggle("hidden", rows.length > 0);

  rows.forEach((tx) => {
    const tr = document.createElement("tr");

    const tdDate = document.createElement("td");
    tdDate.dataset.label = "วันที่";
    tdDate.textContent = tx.date;

    const tdType = document.createElement("td");
    tdType.dataset.label = "ประเภท";
    const badge = document.createElement("span");
    badge.className = `type-badge ${tx.type}`;
    badge.textContent = tx.type === "income" ? "รายรับ" : "รายจ่าย";
    tdType.appendChild(badge);

    const tdCategory = document.createElement("td");
    tdCategory.dataset.label = "หมวดหมู่";
    const catChip = document.createElement("span");
    catChip.className = "category-chip";
    catChip.textContent = `${categoryEmoji(tx.category_name, tx.type)} ${tx.category_name}`;
    tdCategory.appendChild(catChip);

    const tdAmount = document.createElement("td");
    tdAmount.dataset.label = "จำนวนเงิน";
    tdAmount.className = `amount-cell ${tx.type}`;
    tdAmount.textContent = `${tx.type === "expense" ? "-" : "+"}${fmtMoney(tx.amount)}`;

    const tdNote = document.createElement("td");
    tdNote.dataset.label = "บันทึก";
    tdNote.textContent = tx.note || "-";

    const tdSlip = document.createElement("td");
    tdSlip.dataset.label = "สลิป";
    if (tx.slip_url) {
      const link = document.createElement("a");
      link.href = tx.slip_url;
      link.target = "_blank";
      const isImage = /\.(png|jpe?g|webp|gif)$/i.test(tx.slip_filename || "");
      if (isImage) {
        const img = document.createElement("img");
        img.src = tx.slip_url;
        img.className = "slip-thumb";
        link.appendChild(img);
      } else {
        link.textContent = "PDF";
      }
      tdSlip.appendChild(link);
    } else {
      tdSlip.textContent = "-";
    }

    const tdActions = document.createElement("td");
    tdActions.dataset.label = "จัดการ";
    tdActions.className = "row-actions";
    const editBtn = document.createElement("button");
    editBtn.className = "secondary";
    editBtn.textContent = "✏️";
    editBtn.title = "แก้ไข";
    editBtn.onclick = () => startEdit(tx);
    const delBtn = document.createElement("button");
    delBtn.className = "danger";
    delBtn.textContent = "🗑️";
    delBtn.title = "ลบ";
    delBtn.onclick = () => deleteTransaction(tx.id);
    tdActions.appendChild(editBtn);
    tdActions.appendChild(delBtn);

    tr.append(tdDate, tdType, tdCategory, tdAmount, tdNote, tdSlip, tdActions);
    tbody.appendChild(tr);
  });
}

// ---------- Summary ----------

async function loadSummary() {
  const params = new URLSearchParams();
  const month = el("filter-month").value;
  if (month) params.set("month", month);
  const data = await api(`/api/summary?${params.toString()}`);
  el("summary-income").textContent = fmtMoney(data.income_total);
  el("summary-expense").textContent = fmtMoney(data.expense_total);
  el("summary-balance").textContent = fmtMoney(data.balance);
  renderStats(data.by_category);
}

// ---------- Stats (expense breakdown donut) ----------

const CHART_COLORS = [
  "#ef4444", "#f59e0b", "#eab308", "#84cc16", "#06b17a",
  "#0ea5e9", "#6366f1", "#8b5cf6", "#ec4899", "#f97316",
];

function renderStats(byCategory) {
  const expenseCats = byCategory.filter((c) => c.type === "expense" && c.total > 0);
  const donut = el("expense-donut");
  const legend = el("stats-legend");
  const emptyMsg = el("stats-empty");
  legend.innerHTML = "";

  const total = expenseCats.reduce((sum, c) => sum + c.total, 0);
  if (total === 0) {
    donut.style.background = "var(--border)";
    el("donut-total").textContent = "0.00";
    emptyMsg.classList.remove("hidden");
    return;
  }
  emptyMsg.classList.add("hidden");

  let cursor = 0;
  const stops = expenseCats.map((c, i) => {
    const color = CHART_COLORS[i % CHART_COLORS.length];
    const start = cursor;
    cursor += (c.total / total) * 100;
    return `${color} ${start}% ${cursor}%`;
  });
  donut.style.background = `conic-gradient(${stops.join(", ")})`;
  el("donut-total").textContent = fmtMoney(total);

  expenseCats.forEach((c, i) => {
    const color = CHART_COLORS[i % CHART_COLORS.length];
    const pct = ((c.total / total) * 100).toFixed(1);
    const li = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = color;
    const name = document.createElement("span");
    name.className = "legend-name";
    name.textContent = `${categoryEmoji(c.category_name, "expense")} ${c.category_name}`;
    const value = document.createElement("span");
    value.className = "legend-value";
    value.textContent = fmtMoney(c.total);
    const percent = document.createElement("span");
    percent.className = "legend-percent";
    percent.textContent = `${pct}%`;
    li.append(dot, name, value, percent);
    legend.appendChild(li);
  });
}

// ---------- Filters ----------

["filter-month", "filter-type", "filter-category"].forEach((id) => {
  el(id).addEventListener("change", async () => {
    syncQuickFilterChips();
    await Promise.all([loadTransactions(), loadSummary()]);
  });
});

el("clear-filters-btn").addEventListener("click", async () => {
  el("filter-month").value = "";
  el("filter-type").value = "";
  el("filter-category").value = "";
  syncQuickFilterChips();
  await Promise.all([loadTransactions(), loadSummary()]);
});

function currentMonthISO() {
  const d = new Date();
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

function syncQuickFilterChips() {
  const month = el("filter-month").value;
  el("quick-filter-all").classList.toggle("active", !month);
  el("quick-filter-month").classList.toggle("active", month === currentMonthISO());
}

el("quick-filter-all").addEventListener("click", async () => {
  el("filter-month").value = "";
  syncQuickFilterChips();
  await Promise.all([loadTransactions(), loadSummary()]);
});

el("quick-filter-month").addEventListener("click", async () => {
  el("filter-month").value = currentMonthISO();
  syncQuickFilterChips();
  await Promise.all([loadTransactions(), loadSummary()]);
});

// ---------- Google Sheet sync ----------

function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("th-TH", { dateStyle: "medium", timeStyle: "short" });
}

function renderSyncStatus(status) {
  const badge = el("sync-status-badge");
  const configured = Boolean(status.spreadsheet_id && status.has_credentials);
  badge.textContent = configured ? "เชื่อมต่อแล้ว" : "ยังไม่ได้ตั้งค่า";
  badge.classList.toggle("connected", configured);

  if (status.spreadsheet_id) {
    el("sync-spreadsheet-id").value = status.spreadsheet_id;
  }

  const infoParts = [];
  if (status.has_credentials) {
    infoParts.push(`✅ มีไฟล์ credentials แล้ว${status.service_account_email ? ` (${status.service_account_email})` : ""}`);
  } else {
    infoParts.push("⚠️ ยังไม่ได้อัปโหลดไฟล์ credentials");
  }
  el("sync-config-info").textContent = infoParts.join(" ");

  el("sync-last-synced").textContent = status.last_synced_at
    ? `ซิงค์ล่าสุด: ${fmtDateTime(status.last_synced_at)} (${status.last_sync_direction === "push" ? "ขึ้น Sheet" : "ลงเครื่องนี้"})`
    : "ยังไม่เคยซิงค์";
}

async function loadSyncStatus() {
  try {
    const status = await api("/api/sync/config");
    renderSyncStatus(status);
  } catch (err) {
    // sync status is non-critical; ignore failures on load
  }
}

el("sync-config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  el("sync-error").classList.add("hidden");
  const fd = new FormData();
  fd.append("spreadsheet_id", el("sync-spreadsheet-id").value.trim());
  const file = el("sync-credentials-file").files[0];
  if (file) fd.append("credentials", file);
  try {
    const status = await api("/api/sync/config", { method: "POST", body: fd });
    renderSyncStatus(status);
    el("sync-credentials-file").value = "";
  } catch (err) {
    el("sync-error").textContent = err.message;
    el("sync-error").classList.remove("hidden");
  }
});

el("sync-push-btn").addEventListener("click", async () => {
  el("sync-error").classList.add("hidden");
  const btn = el("sync-push-btn");
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ กำลังซิงค์ขึ้น...";
  try {
    const result = await api("/api/sync/push", { method: "POST" });
    renderSyncStatus(result);
  } catch (err) {
    el("sync-error").textContent = err.message;
    el("sync-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
});

el("sync-pull-btn").addEventListener("click", async () => {
  if (!confirm("การดึงข้อมูลจาก Google Sheet จะเขียนทับรายการทั้งหมดในเครื่องนี้ ต้องการดำเนินการต่อหรือไม่?")) {
    return;
  }
  el("sync-error").classList.add("hidden");
  const btn = el("sync-pull-btn");
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ กำลังดึงข้อมูล...";
  try {
    const result = await api("/api/sync/pull", { method: "POST" });
    renderSyncStatus(result);
    resetForm();
    await loadCategories();
    await Promise.all([loadTransactions(), loadSummary()]);
  } catch (err) {
    el("sync-error").textContent = err.message;
    el("sync-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
});

// ---------- Auth ----------

el("logout-btn").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
});

// ---------- Bottom nav ----------

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    if (target.tagName === "DETAILS") target.open = true;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

// ---------- Init ----------

async function init() {
  el("tx-date").value = todayISO();
  syncQuickFilterChips();
  await loadCategories();
  await Promise.all([loadTransactions(), loadSummary()]);
  await loadSyncStatus();
}

init();
