const $ = (selector) => document.querySelector(selector);

function show(target, value){
  if(typeof value === "string"){ target.textContent = value; return; }
  target.textContent = JSON.stringify(value, null, 2);
}

async function post(url, body){
  const response = await fetch(url, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(body ?? {})});
  const data = await response.json().catch(() => ({}));
  if(!response.ok) throw new Error(data.detail?.message || data.detail || "请求失败");
  return data;
}

async function get(url){
  const response = await fetch(url);
  const data = await response.json().catch(() => ({}));
  if(!response.ok) throw new Error(data.detail?.message || data.detail || "请求失败");
  return data;
}

$("#csv-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = $("#csv-result");
  const revision = $("#expected-revision").value;
  const payload = {csv: $("#csv").value};
  if(revision !== "") payload.expected_revision = Number(revision);
  show(out, "导入中…");
  try{
    const data = await post("/api/tax/invoices/import-csv", payload);
    show(out, data);
    if(data.imported > 0) setTimeout(() => location.reload(), 700);
  }catch(error){ show(out, `失败：${error.message}`); }
});

$("#policy-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = $("#policy-result");
  const payload = {supplier_code: $("#policy-supplier").value.trim(), convention: $("#policy-convention").value};
  const revision = $("#policy-revision").value;
  if(revision !== "") payload.expected_revision = Number(revision);
  try{
    const data = await post("/api/tax/supplier/convention", payload);
    show(out, data);
    if(data.changed) setTimeout(() => location.reload(), 700);
  }catch(error){ show(out, `失败：${error.message}`); }
});

$("#review-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = $("#review-result");
  const supplier = encodeURIComponent($("#review-supplier").value.trim());
  const doc = encodeURIComponent($("#review-doc").value.trim());
  try{ show(out, await get(`/api/tax/invoices/${supplier}/${doc}/review`)); }
  catch(error){ show(out, `失败：${error.message}`); }
});

$("#resume-legacy")?.addEventListener("click", async () => {
  const button = $("#resume-legacy");
  button.disabled = true;
  show($("#legacy-feedback"), "恢复中…");
  try{
    const data = await post("/api/tax/legacy/migrate", {});
    show($("#legacy-feedback"), `已迁移 ${data.migrated.length} 条，拒绝 ${data.rejected.length} 条，剩余 ${data.pending} 条`);
    if(data.complete) setTimeout(() => location.reload(), 700);
  }catch(error){ show($("#legacy-feedback"), `失败：${error.message}`); }
  finally{ button.disabled = false; }
});
