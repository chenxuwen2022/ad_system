
let advertiserAccounts = [];

window.addEventListener("load", function(){
    loadAdvertiserSelects();
    loadLocalMaterials();
    initUploadZone();
});

// ===== 广告主下拉框（内容来自后台保存的账户，只显示真实店铺） =====
async function loadAdvertiserSelects(){
    try{
        const res = await (await fetch("/api/advertiser_accounts")).json();
        advertiserAccounts = (res.success && res.data) ? res.data : [];
    }catch(e){
        advertiserAccounts = [];
    }
    const hasAccounts = advertiserAccounts.length > 0;
    ["matAdvertiser", "popAdvertiser"].forEach(id=>{
        const sel = document.getElementById(id);
        const prev = sel.value;
        sel.innerHTML = "";
        if(!hasAccounts){
            const ph = document.createElement("option");
            ph.value = ""; ph.textContent = "暂无账户，请点击右上角「设置」添加";
            ph.disabled = true;
            sel.appendChild(ph);
            return;
        }
        advertiserAccounts.forEach(a=>{
            const opt = document.createElement("option");
            opt.value = a.advertiser_id;
            opt.textContent = a.name ? `${a.name}（${a.advertiser_id}）` : a.advertiser_id;
            sel.appendChild(opt);
        });
        // 原选中账户仍在列表中则保持，否则默认选第一个
        if(prev && advertiserAccounts.some(a=>String(a.advertiser_id)===prev)){
            sel.value = prev;
        }
    });
    if(hasAccounts){
        loadMaterialList();
    }else{
        document.getElementById("materialSelect").innerHTML = "<option>请先添加广告主账户</option>";
        document.getElementById("matDetail").innerHTML = "";
    }
}

function onMatAdvertiserChange(){
    document.getElementById("matDetail").innerHTML = "";
    loadMaterialList();
}

function setBusy(btn, busy, busyText){
    btn.disabled = busy;
    if(!btn.dataset.normalText){ btn.dataset.normalText = btn.innerText; }
    btn.innerText = busy ? busyText : btn.dataset.normalText;
}

// ===== ①·本地素材库（遍历 C:\test素材，预览 + 弹窗投放） =====
function fmtSize(b){
    if(b >= 1024*1024){ return (b/1024/1024).toFixed(1) + " MB"; }
    if(b >= 1024){ return (b/1024).toFixed(0) + " KB"; }
    return b + " B";
}
async function loadLocalMaterials(){
    const box = document.getElementById("localMaterialBox");
    box.innerHTML = "<p class='spin'>遍历目录中…</p>";
    try{
        await loadLaunchStatus();
        const resp = await fetch("/api/local_materials");
        const res = await resp.json();
        if(!res.success){ box.innerHTML = "<p style='color:red'>"+(res.error||"加载失败")+"</p>"; return; }
        if(res.error){ box.innerHTML = "<p style='color:#e02424'>"+esc(res.error)+"</p>"; return; }
        renderLocalMaterials(res.data);
    }catch(e){
        box.innerHTML = "<p style='color:#e02424'>请求失败："+e+"</p>";
    }
}
// ===== 上传素材库：上传功能区 + 已上传素材 =====
function initUploadZone(){
    const zone = document.getElementById("uploadZone");
    if(!zone) return;
    zone.onclick = function(){ document.getElementById("uploadInput").click(); };
    zone.ondragover = function(e){ e.preventDefault(); zone.style.borderColor = "#1f6feb"; zone.style.background = "#f4f8ff"; };
    zone.ondragleave = function(){ zone.style.borderColor = ""; zone.style.background = ""; };
    zone.ondrop = function(e){
        e.preventDefault();
        zone.style.borderColor = ""; zone.style.background = "";
        uploadFiles(Array.from(e.dataTransfer.files || []), zone);
    };
    document.getElementById("uploadInput").onchange = function(){
        const inp = this;
        uploadFiles(Array.from(inp.files || []), zone);
        inp.value = "";
    };
}
async function uploadFiles(files, zone){
    const good = files.filter(f=>/\\.(jpg|jpeg|png|gif|webp|bmp|mp4|mov|avi|mkv|webm)$/i.test(f.name));
    if(!good.length){ alert("仅支持图片、视频文件"); return; }
    const tip = zone ? zone.querySelector(".uc-tip") : null;
    let ok = 0;
    for(let i=0;i<good.length;i++){
        if(tip){ tip.style.display = "block"; tip.textContent = `上传中 ${i+1}/${good.length}…`; }
        const fd = new FormData();
        fd.append("file", good[i]);
        try{
            const r = await fetch("/api/upload_material", {method:"POST", body:fd});
            const j = await r.json();
            if(j.success){ ok++; }
        }catch(e){ /* 单个失败继续 */ }
    }
    if(tip){ tip.style.display = "none"; }
    loadUploadedMaterials();
    if(ok < good.length){
        alert(`成功上传 ${ok} 个，${good.length-ok} 个失败（格式不支持或目录不可写）`);
    }
}

// ===== Tab：本地素材库 / 上传素材库 =====
function switchLib(name){
    document.getElementById("tabLocal").classList.toggle("on", name === "local");
    document.getElementById("tabUpload").classList.toggle("on", name === "upload");
    document.getElementById("libLocal").style.display = name === "local" ? "" : "none";
    document.getElementById("libUpload").style.display = name === "upload" ? "" : "none";
    if(name === "upload"){ loadUploadedMaterials(); }
}
function fmtTime(ts){
    if(!ts) return "";
    const d = new Date(ts * 1000);
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function renderMaterialGrid(box, items, urlPrefix){
    if(!items.length){ box.innerHTML = "<p class='muted'>暂无素材</p>"; return; }
    const grid = document.createElement("div");
    grid.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;";
    items.forEach(it=>{
        const url = urlPrefix + encodeURIComponent(it.name);
        const sel = selectedMaterials.some(x=>x.path===it.path);
        const card = document.createElement("div");
        card.className = "mat-card" + (sel ? " sel" : "");
        card.setAttribute("data-path", it.path);
        card.style.cssText = "border:1px solid #e5e6eb;border-radius:8px;overflow:hidden;background:#fff;display:flex;flex-direction:column;";
        let mediaHtml = "";
        if(it.type === "video"){
            mediaHtml = `<video src="${url}" controls preload="metadata" style="width:100%;height:150px;object-fit:contain;background:#000"></video>`;
        }else{
            mediaHtml = `<img src="${url}" style="width:100%;height:150px;object-fit:contain;background:#f7f8fa" loading="lazy">`;
        }
        const tag = it.type === "video" ? "<span style='color:#c96442'>视频</span>" : "<span style='color:#3370ff'>图片</span>";
        const timeTxt = fmtTime(it.mtime);
        const badge = launchBadge(it);
        card.innerHTML =
            `<span class="mat-check${sel ? " on" : ""}" title="勾选后批量投放"
                  onclick='event.stopPropagation();toggleMaterial(${JSON.stringify({path:it.path,name:it.name,type:it.type})})'>${sel ? "✓" : ""}</span>` +
            (badge ? `<span class="launch-badge ${badge.cls}" title="${badge.tip}">${badge.text}</span>` : "") +
            mediaHtml +
            `<div style="padding:8px 10px;flex:1;display:flex;flex-direction:column;gap:4px">
                <div style="font-size:13px;word-break:break-all" title="${esc(it.name)}">${esc(it.name)}</div>
                <div class="muted" style="font-size:12px">${tag} · ${fmtSize(it.size)}${timeTxt ? " · " + timeTxt : ""}</div>
                <button onclick='openLaunchModal(${JSON.stringify(it.path)}, ${JSON.stringify(it.name)})'
                        style="margin-top:auto;padding:6px 0;font-size:13px">投放</button>
            </div>`;
        grid.appendChild(card);
    });
    box.innerHTML = "";
    box.appendChild(grid);
}
function renderLocalMaterials(items){
    renderMaterialGrid(document.getElementById("localMaterialBox"), items, "/api/local_media/");
}
// 素材投放状态徽标：未投放返回空；否则按最近一次结果显示
function launchBadge(it){
    const st = launchStatusMap[it.path];
    if(!st){ return ""; }
    const cnt = st.count > 1 ? " " + st.count + "次" : "";
    const t = st.time ? " · " + st.time : "";
    let cls, text;
    if(st.status === "fail"){
        cls = "fail"; text = "投放失败" + (st.mode === "test" ? "（测试）" : "") + t;
    }else if(st.mode === "test"){
        cls = "test"; text = "测试投放" + cnt + t;
    }else{
        cls = "ok"; text = "已投放" + cnt + t;
    }
    const tip = "最近投放：" + (st.time || "—") +
        (st.plan_name ? "，计划：" + st.plan_name : "") +
        (st.product_id ? "，商品：" + st.product_id : "") +
        (st.detail ? "，" + st.detail : "");
    return {cls: cls, text: text, tip: tip};
}
// ===== 素材勾选（跨本地/上传库）与批量投放入口 =====
function toggleMaterial(it){
    const i = selectedMaterials.findIndex(x=>x.path===it.path);
    if(i >= 0){ selectedMaterials.splice(i, 1); }
    else{ selectedMaterials.push({path:it.path, name:it.name, type:it.type||""}); }
    updateBatchBtn();
    refreshCardSel(it.path);
}
function refreshCardSel(path){
    document.querySelectorAll(".mat-card[data-path]").forEach(c=>{
        if(c.getAttribute("data-path") === path){
            const on = selectedMaterials.some(x=>x.path===path);
            c.classList.toggle("sel", on);
            const chk = c.querySelector(".mat-check");
            if(chk){ chk.classList.toggle("on", on); chk.textContent = on ? "✓" : ""; }
        }
    });
}
function updateBatchBtn(){
    const btn = document.getElementById("batchLaunchBtn");
    if(!btn) return;
    const n = selectedMaterials.length;
    btn.style.display = n ? "" : "none";
    btn.textContent = "批量投放(" + n + ")";
}
function openBatchLaunchModal(){
    if(!selectedMaterials.length){ alert("请先在素材库勾选要投放的素材（点击卡片左上角圆圈）"); return; }
    openLaunchModalWith(selectedMaterials.slice());
}
async function loadUploadedMaterials(){
    const box = document.getElementById("uploadedMaterialBox");
    box.innerHTML = "<p class='spin'>加载中…</p>";
    try{
        await loadLaunchStatus();
        const res = await (await fetch("/api/uploaded_materials")).json();
        if(!res.success){ box.innerHTML = "<p style='color:#e02424'>"+(res.error||"加载失败")+"</p>"; return; }
        renderMaterialGrid(box, res.data || [], "/api/uploaded_media/");
    }catch(e){
        box.innerHTML = "<p style='color:#e02424'>请求失败："+e+"</p>";
    }
}

// ===== 弹窗：选择商品并投放 =====
let popLocalFilePath = "";
let popLocalFilePaths = []; // 批量投放：本次选中的全部素材 [{path,name,type}]
let launchStatusMap = {};   // 素材投放状态：{file_path: {status,mode,count,time,...}}
async function loadLaunchStatus(){
    try{
        const res = await (await fetch("/api/material_launch_status")).json();
        launchStatusMap = (res.success && res.data) ? res.data : {};
    }catch(e){ launchStatusMap = {}; }
}
let selectedMaterials = []; // 素材库中已勾选的素材（跨本地/上传两个库）
let popTagList = [];        // 标签设置里的全部标签
let popSelectedTags = [];   // 本次投放选中的标签（多选）
function openLaunchModal(path, name){
    openLaunchModalWith([{path:path, name:name, type:""}]);
}
function openLaunchModalWith(mats){
    popLocalFilePaths = mats;
    popLocalFilePath = mats.length ? mats[0].path : "";
    document.getElementById("popFileName").textContent = mats.length > 1
        ? `共 ${mats.length} 个素材`
        : (mats[0] ? mats[0].name : "");
    document.getElementById("popLaunchResult").style.display = "none";
    document.getElementById("launchModal").classList.add("show");
    if(advertiserAccounts.length && !document.getElementById("popAdvertiser").value){
        const sel = document.getElementById("popAdvertiser");
        sel.value = advertiserAccounts[0].advertiser_id;
    }
    loadPopProducts();
    loadPopPlans();
    loadPopTags();
    loadPopMarks();
}
// 加载标签列表并渲染为可多选的 chips
async function loadPopTags(){
    const box = document.getElementById("popTagBox");
    try{
        const res = await (await fetch("/api/tags")).json();
        popTagList = (res.success && res.data) ? res.data : [];
        renderPopTags();
    }catch(e){
        box.innerHTML = "<span class='lm-tags-empty'>标签加载失败</span>";
    }
}
function renderPopTags(){
    const box = document.getElementById("popTagBox");
    if(!popTagList.length){
        box.innerHTML = "<span class='lm-tags-empty'>暂无标签，请先在右上角「标签设置」添加</span>";
        return;
    }
    box.innerHTML = "";
    popTagList.forEach(t=>{
        const chip = document.createElement("span");
        chip.className = "lm-tag" + (popSelectedTags.includes(t.name) ? " on" : "");
        chip.innerHTML = `<span class="dot" style="background:${esc(t.color)}"></span>${esc(t.name)}`;
        chip.onclick = function(){
            const i = popSelectedTags.indexOf(t.name);
            if(i >= 0){ popSelectedTags.splice(i, 1); }
            else{ popSelectedTags.push(t.name); }
            renderPopTags();
        };
        box.appendChild(chip);
    });
}
// 回显该素材已保存的标签标记
async function loadPopMarks(){
    if(!popLocalFilePath) return;
    try{
        const res = await (await fetch("/api/material_marks?file_path=" + encodeURIComponent(popLocalFilePath))).json();
        if(res.success && res.data && res.data.tags){
            popSelectedTags = res.data.tags.slice();
        }
        renderPopTags();
    }catch(e){ /* 静默 */ }
}
function closeLaunchModal(){
    document.getElementById("launchModal").classList.remove("show");
}
function onPopAdvertiserChange(){
    loadPopProducts();
    loadPopPlans();
}
// ===== 投放计划下拉框 + 预算汇总（该店铺全部计划） =====
let popPlans = [];
function fmtMoney(x){
    return "¥" + Number(x||0).toLocaleString("zh-CN", {minimumFractionDigits:0, maximumFractionDigits:2});
}
async function loadPopPlans(force){
    const aid = document.getElementById("popAdvertiser").value;
    const sel = document.getElementById("popPlanSelect");
    const sum = document.getElementById("popPlanSummary");
    if(!aid){
        sel.innerHTML = "<option>请先选择店铺</option>";
        sum.innerHTML = "";
        return;
    }
    sel.innerHTML = "<option>" + (force ? "正在从千川拉取最新计划（约10-30秒）…" : "加载中…") + "</option>";
    sum.innerHTML = "";
    try{
        let url = "/api/plans?advertiser_id=" + encodeURIComponent(aid);
        if(force){ url += "&refresh=1"; }
        const resp = await fetch(url);
        const res = await resp.json();
        if(!res.success){
            sel.innerHTML = "<option>加载失败：" + esc(res.error||"") + "</option>";
            sum.innerHTML = "";
            return;
        }
        popPlans = res.plans || [];
        renderPopPlanOptions();
        renderPopPlanSummary(res);
    }catch(e){
        sel.innerHTML = "<option>加载失败</option>";
        sum.innerHTML = "";
    }
}
function renderPopPlanOptions(){
    const sel = document.getElementById("popPlanSelect");
    const kw = (document.getElementById("popPlanSearch").value||"").trim().toLowerCase();
    sel.innerHTML = "";
    const matched = kw ? popPlans.filter(p=>(p.name||"").toLowerCase().includes(kw)) : popPlans;
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = `共${popPlans.length}个投放计划`+(kw?`，筛选出${matched.length}个`:"");
    sel.appendChild(ph);
    matched.slice(0, 500).forEach(p=>{
        const o = document.createElement("option");
        o.value = p.ad_id;
        o.textContent = `[${p.status_text}] ${p.name}（日预算${fmtMoney(p.budget)}，消耗${fmtMoney(p.cost)}）`;
        sel.appendChild(o);
    });
    if(matched.length > 500){
        const tip = document.createElement("option");
        tip.value = ""; tip.textContent = `…共${matched.length}个，仅显示前500个，请输入关键词筛选`;
        sel.appendChild(tip);
    }
}
function filterPopPlans(){ renderPopPlanOptions(); }
function renderPopPlanSummary(res){
    const sum = document.getElementById("popPlanSummary");
    sum.innerHTML =
        `<div class="k"><div class="lab">投放总量（日预算）</div><div class="val">${fmtMoney(res.total_budget)}</div></div>` +
        `<div class="k"><div class="lab">已消耗（90天）</div><div class="val">${fmtMoney(res.total_cost)}</div></div>` +
        `<div class="k"><div class="lab">剩余投放量</div><div class="val hl">${fmtMoney(res.remain_budget)}</div></div>` +
        `<div class="k"><div class="lab">投放中计划</div><div class="val">${res.plan_count} 个</div></div>`;
}
let popProducts = [];
async function loadPopProducts(force){
    const aid = document.getElementById("popAdvertiser").value;
    if(!aid){ return; }
    const sel = document.getElementById("popProductSelect");
    sel.innerHTML = "<option>" + (force ? "正在从千川拉取最新商品（约10-30秒）…" : "加载中…") + "</option>";
    try{
        let url = "/api/products?advertiser_id=" + encodeURIComponent(aid);
        if(force){ url += "&refresh=1"; }
        const resp = await fetch(url);
        const res = await resp.json();
        if(!res.success){ sel.innerHTML = "<option>加载失败："+res.error+"</option>"; return; }
        popProducts = res.data || [];
        renderPopProductOptions();
    }catch(e){
        sel.innerHTML = "<option>加载失败</option>";
    }
}
function renderPopProductOptions(){
    const sel = document.getElementById("popProductSelect");
    const kw = (document.getElementById("popProductSearch").value||"").trim().toLowerCase();
    sel.innerHTML = "";
    const matched = kw ? popProducts.filter(p=>(p.name||"").toLowerCase().includes(kw)) : popProducts;
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = `共${popProducts.length}个商品`+(kw?`，筛选出${matched.length}个`:"");
    sel.appendChild(ph);
    matched.slice(0, 500).forEach(p=>{
        const o = document.createElement("option");
        o.value = p.id;
        const mc = p.material_count ? `（已挂${p.material_count}素材）` : "";
        o.textContent = p.name + mc;
        sel.appendChild(o);
    });
    if(matched.length > 500){
        const tip = document.createElement("option");
        tip.value = ""; tip.textContent = `…共${matched.length}个，仅显示前500个，请输入关键词筛选`;
        sel.appendChild(tip);
    }
}
function filterPopProducts(){ renderPopProductOptions(); }
async function popLaunchAd(){
    const pid = document.getElementById("popProductSelect").value;
    if(!pid){ alert("请选择投放商品"); return; }
    const planId = document.getElementById("popPlanSelect").value;
    if(!planId){ alert("请先选择投放计划（素材必须投放到所选计划下，不会新建计划）"); return; }
    const btn = document.getElementById("popLaunchBtn");
    if(btn.disabled) return;
    const mats = (popLocalFilePaths && popLocalFilePaths.length) ? popLocalFilePaths : [];
    if(!mats.length){ alert("未选择素材"); return; }
    const testMode = document.getElementById("popTestMode").checked;
    setBusy(btn, true, testMode ? "测试模式：模拟投放中…" : "投放中，请稍候…");
    const lr = document.getElementById("popLaunchResult");
    lr.style.display = "block";
    lr.className = "lm-result";
    lr.style.color = "#55637a";
    lr.style.background = "#f6f8fb";
    lr.style.border = "1px solid #e4ebf4";
    lr.style.whiteSpace = "normal";
    const total = mats.length;
    const results = [];
    for(let i=0;i<total;i++){
        const m = mats[i];
        lr.innerText = (testMode ? "【测试模式】" : "") + `正在投放素材 ${i+1}/${total}：${m.name} ${testMode ? "（模拟）" : "（约10-30秒）"}…`;
        lr.scrollIntoView({block:"nearest"});
        try{
            const payload = {
                "platform":"douyin",
                "local_file_path": m.path,
                "product_ids": [pid],
                "advertiser_id": document.getElementById("popAdvertiser").value || null,
                "plan_id": planId,
                "tags": popSelectedTags,
                "test_mode": testMode
            };
            const resp = await fetch("/api/ad/launch", {
                method:"POST",
                headers:{"Content-Type":"application/json"},
                body:JSON.stringify(payload)
            });
            const res = await resp.json();
            results.push({name:m.name, ok:!!res.success, res:res});
        }catch(e){
            results.push({name:m.name, ok:false, res:{success:false, error:String(e)}});
        }
    }
    const okN = results.filter(r=>r.ok).length;
    let html = `<div class="lm-batch-summary">批量投放完成：成功 ${okN}/${total}${testMode ? "（测试模式，未产生真实费用）" : ""}</div>`;
    results.forEach((r,i)=>{
        const brief = r.ok && r.res && r.res.data ? (r.res.data.error_msg || r.res.msg || "") : (r.res.error || "");
        html += `<div class="lm-batch-item ${r.ok?"ok":"err"}">
            <b>${i+1}. ${esc(r.name)}</b> — ${r.ok ? "✅ 成功" : "❌ 失败"}${brief ? `　<span style="color:#8a97ab">${esc(brief)}</span>` : ""}
            <pre>${esc(JSON.stringify(r.res,null,2))}</pre>
        </div>`;
    });
    lr.removeAttribute("style");
    lr.className = "lm-result " + (okN===total ? "ok" : "err");
    lr.innerHTML = html;
    lr.scrollIntoView({block:"nearest"});
    setBusy(btn, false);
    // 投放完成后刷新素材库中的投放状态徽标
    await loadLaunchStatus();
    const localTab = document.getElementById("tabLocal");
    if(localTab && localTab.classList.contains("on")){ loadLocalMaterials(); }
    else{ loadUploadedMaterials(); }
}

// ===== 单素材深度分析 =====
let materialOptions = [];
async function loadMaterialList(force){
    const sel = document.getElementById("materialSelect");
    sel.innerHTML = "<option>" + (force ? "正在从千川拉取最新数据（约40秒）…" : "加载中…") + "</option>";
    const aid = document.getElementById("matAdvertiser").value;
    try{
        let url = "/api/material_list" + (aid ? ("?advertiser_id="+encodeURIComponent(aid)) : "");
        if(force){ url += (aid ? "&" : "?") + "refresh=1"; }
        const resp = await fetch(url);
        const res = await resp.json();
        if(!res.success){ sel.innerHTML = "<option>加载失败："+res.error+"</option>"; return; }
        materialOptions = res.data;
        renderMaterialOptions();
    }catch(e){
        sel.innerHTML = "<option>加载失败</option>";
    }
}

function renderMaterialOptions(){
    const sel = document.getElementById("materialSelect");
    const kw = (document.getElementById("materialSearch").value||"").trim().toLowerCase();
    sel.innerHTML = "";
    const matched = kw
        ? materialOptions.filter(m=>m.name.toLowerCase().includes(kw))
        : materialOptions;
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = `共${materialOptions.length}个素材`+(kw?`，筛选出${matched.length}个`:"");
    sel.appendChild(ph);
    // 万级素材只渲染前500个，避免下拉框卡死；搜索筛选在全量数据上进行
    const MAX_SHOW = 500;
    matched.slice(0, MAX_SHOW).forEach(m=>{
        const o = document.createElement("option");
        o.value = m.id;
        o.textContent = `[${m.type}] ${m.name}（消耗${m.消耗}元）`;
        sel.appendChild(o);
    });
    if(matched.length > MAX_SHOW){
        const tip = document.createElement("option");
        tip.value = "";
        tip.textContent = `…共${matched.length}个，仅显示前${MAX_SHOW}个，请输入关键词筛选`;
        sel.appendChild(tip);
    }
}

function filterMaterials(){ renderMaterialOptions(); }

async function loadMaterialDetail(){
    const mid = document.getElementById("materialSelect").value;
    const box = document.getElementById("matDetail");
    if(!mid){ box.innerHTML = ""; return; }
    const aid = document.getElementById("matAdvertiser").value;
    box.innerHTML = "<p class='spin'>加载素材逐日数据并调用 AI 分析中（约20-40秒）…</p>";
    try{
        const resp = await fetch("/api/material_detail?material_id=" + encodeURIComponent(mid) + (aid ? ("&advertiser_id="+encodeURIComponent(aid)) : ""));
        const res = await resp.json();
        if(!res.success){ box.innerHTML = "<p style='color:red'>"+(res.error||"查询失败")+"</p>"; return; }
        const s = res.summary, daily = res.daily || [], pv = res.preview || {};
        let html = "<div class='preview-box'>";
        if(pv.kind === "video" && pv.url){
            html += `<video src="${pv.url}" poster="${pv.poster||''}" controls></video>`;
        }else if(pv.kind === "image" && pv.url){
            html += `<img src="${pv.url}">`;
        }else{
            html += `<div class='muted' style='max-width:300px'>（该素材预览暂不可用，可能为共享素材或限流）</div>`;
        }
        html += "</div>";
        html += `<div style="margin-top:12px"><b style="font-size:14px">${esc(s.name)}（${s.type}）</b></div>`;
        if(s.material_status || s.audit_status || (s.products && s.products.length)){
            html += `<div class='muted' style='margin-top:6px'>素材状态：${esc(s.material_status||"—")}　|　审核状态：${esc(s.audit_status||"—")}${s.material_types&&s.material_types.length?("　|　类型："+esc(s.material_types.join("/"))):""}${s.products&&s.products.length?("　|　关联商品 "+s.products.length+" 个"):""}</div>`;
        }
        if(s.metrics_all && s.metrics_all.length){
            html += "<details open style='margin-top:14px'><summary class='muted'>全部报表指标（点击收起，共 "+s.metrics_all.length+" 项）</summary>";
            html += "<div class='kpi kpi-all'>";
            s.metrics_all.forEach(m=>{
                html += `<div class='cell'><div class='lab'>${esc(m.cn)}</div><div class='val'>${esc(m.value)}</div></div>`;
            });
            html += "</div>";
            // CVR、投放天数、近7天趋势：跟随全部报表指标展示
            html += "<div class='kpi kpi-all' style='margin-top:8px'>";
            html += kpi("CVR", s.转化率+"%");
            html += kpi("投放天数", s.active_days+"天");
            html += kpi("近7天趋势", s.trend);
            html += "</div></details>";
        }
        if(daily.length){
            const maxC = Math.max(...daily.map(d=>d.cost), 1);
            html += "<h4 style='margin-top:16px;font-size:14px'>逐日消耗曲线（近"+daily.length+"天）</h4>";
            html += "<div class='barwrap'>";
            daily.forEach(d=>{
                const h = Math.round(d.cost/maxC*120);
                html += `<div class='bar' title="${d.date} 消耗${d.cost} 成交${d.orders}单" style="height:${h}px"></div>`;
            });
            html += "</div>";
            html += "<details style='margin-top:10px'><summary class='muted'>逐日明细（点击展开）</summary>";
            html += "<table class='data'><tr><th>日期</th><th>消耗</th><th>展示</th><th>点击</th><th>成交单</th><th>成交金额</th></tr>";
            daily.forEach(d=>{
                html += `<tr><td>${d.date}</td><td>${d.cost}</td><td>${d.show}</td><td>${d.click}</td><td>${d.orders}</td><td>${d.gmv}</td></tr>`;
            });
            html += "</table></details>";
        }
        html += "<div class='ai-head'>AI 点评与修改建议</div>";
        html += "<div class='ai-box'>" + renderAiViz(res.ai, s) + "</div>";
        box.innerHTML = html;
    }catch(e){
        box.innerHTML = "<p style='color:#e02424'>请求失败："+e+"</p>";
    }
}
function kpi(lab, val){ return `<div class='cell'><div class='lab'>${lab}</div><div class='val'>${val}</div></div>`; }
function esc(t){ return String(t==null?"":t).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

// ===== AI 点评文本美化：识别"一、二、"分节、编号条目与 **加粗**，渲染成卡片式布局 =====
function aiInline(s){
    // 已 esc 后的文本里再把 **加粗** 转成 <b>（esc 已转义 < >，此处安全）
    // Python 层写双反斜杠，输出到 JS 为正则字面量（匹配字面双星号）
    return esc(s).replace(/\\*\\*(.+?)\\*\\*/g, "<b>$1</b>");
}
// ===== AI 点评可视化：诊断概览（评分条+阶段徽章） + 建议卡片 =====
function judgeStage(s){
    const days = Number(s.active_days) || 0;
    const roi = parseFloat(s.支付ROI) || 0;
    const trend = s.trend || "";
    if(days <= 3){ return {name:"冷启动", cls:"cold"}; }
    if(trend === "上升" && roi >= 1){ return {name:"起量期", cls:"up"}; }
    if(trend === "下滑" && roi < 1){ return {name:"衰退期", cls:"down"}; }
    return {name:"稳定期", cls:"stable"};
}
function gradeOf(v, t1, t2, t3){
    if(v >= t1){ return {g:"优", c:"#16a34a"}; }
    if(v >= t2){ return {g:"良", c:"#1f6feb"}; }
    if(v >= t3){ return {g:"中", c:"#d97706"}; }
    return {g:"差", c:"#dc2626"};
}
function scoreBar(label, val, pct, grade, color, unit){
    return `<div class="viz-bar" title="${label} ${val}${unit||""}">
        <div class="viz-lab">${label}</div>
        <div class="viz-track"><div class="viz-fill" style="width:${Math.max(4, Math.min(100, pct))}%;background:${color}"></div></div>
        <div class="viz-grade" style="color:${color}">${grade}</div>
        <div class="viz-val">${val}${unit||""}</div>
    </div>`;
}
function renderAiViz(text, s){
    // 1) 诊断概览：阶段徽章 + 四维评分条（全部可视化）
    const stage = judgeStage(s);
    const ctr = parseFloat(s.点击率) || 0;
    const cvr = parseFloat(s.转化率) || 0;
    const roi = parseFloat(s.支付ROI) || 0;
    const trend = s.trend || "平稳";
    const trendMeta = trend === "上升" ? {c:"#16a34a", p:100}
        : (trend === "下滑" ? {c:"#dc2626", p:30} : {c:"#1f6feb", p:60});
    const gCtr = gradeOf(ctr, 3, 1.5, 0.8);
    const gCvr = gradeOf(cvr, 2, 1, 0.5);
    const gRoi = gradeOf(roi, 1.5, 1.2, 0.8);
    let html = `<div class="viz-head">
        <span class="viz-stage ${stage.cls}">${stage.name}</span>
        <span class="viz-note" style="margin:0">累计投放 <b>${s.active_days||0}</b> 天 · 近7天消耗 <b>${fmtMoney(s.recent7_cost||0)}</b>${s.trend ? " · 趋势 <b>"+esc(s.trend)+"</b>" : ""}</span>
    </div>`;
    html += `<div class="viz-grid">`;
    html += scoreBar("CTR", ctr.toFixed(2), ctr/4*100, gCtr.g, gCtr.c, "%");
    html += scoreBar("CVR", cvr.toFixed(2), cvr/3*100, gCvr.g, gCvr.c, "%");
    html += scoreBar("ROI", roi.toFixed(2), roi/2*100, gRoi.g, gRoi.c, "");
    html += scoreBar("消耗趋势", trend, trendMeta.p, trend, trendMeta.c, "");
    html += `</div>`;
    // 2) AI 结论 → 全可视化：诊断要点标签流 + 建议卡片网格（无段落文字）
    html += renderAiVisual(text);
    return html;
}
// 把 AI 文本解析为可视化元素：结论章节→彩色标签流，建议章节→卡片网格
function renderAiVisual(text){
    const raw = String(text==null?"":text).trim();
    if(!raw) return "<div class='muted'>（无返回）</div>";
    const lines = raw.split(/\r?\n/).map(l=>l.trim()).filter(l=>l.length>0);
    if(!lines.length) return "<div class='muted'>（无返回）</div>";
    const secs = [];
    let cur = null;
    lines.forEach(line=>{
        const m = line.match(/^(第?[一二三四五六七八九十百]+)[、.．:：)）]\s*(.+)$/);
        if(m){
            cur = {title: m[2].trim(), items: []};
            secs.push(cur);
        }else{
            if(!cur){ cur = {title: "AI 结论", items: []}; secs.push(cur); }
            cur.items.push(line);
        }
    });
    if(!secs.length){ secs.push({title: "AI 结论", items: lines}); }
    let html = "";
    secs.forEach(sec=>{
        html += `<div class="viz-sec"><span class="viz-sec-tag">${esc(sec.title)}</span></div>`;
        // 提取条目
        const cards = [];
        sec.items.forEach(line=>{
            const num = line.match(/^(\d+)[、.．:：)）]\s*(.+)$/);
            const circle = line.match(/^([①②③④⑤⑥⑦⑧⑨⑩])\s*(.+)$/);
            const bold = line.match(/^\*\*(.+?)\*\*$/);
            if(num){ cards.push({t: num[2].trim()}); }
            else if(circle){ cards.push({t: circle[2].trim()}); }
            else if(bold){ cards.push({t: bold[1].trim()}); }
            else if(line.length){ cards.push({t: line}); }
        });
        const isAdvice = /建议|优化|修改|改法|方向|行动|提升|加强|关注/.test(sec.title);
        if(isAdvice){
            html += `<div class="viz-cards">`;
            cards.forEach((c,i)=>{
                html += `<div class="viz-card"><span class="viz-no">${i+1}</span><span class="viz-txt">${aiInline(c.t)}</span></div>`;
            });
            html += `</div>`;
        }else{
            html += `<div class="viz-chips">`;
            cards.forEach(c=>{
                html += `<span class="viz-chip">${aiInline(c.t)}</span>`;
            });
            html += `</div>`;
        }
    });
    return html;
}
// Tab 内容区行渲染：编号条目 / 圆号条目 / 普通行
function renderAiTabLines(lines){
    let html = "";
    lines.forEach(line=>{
        const numItem = line.match(/^(\\d+)[、.．:：)）]\\s*(.+)$/);
        const circleItem = line.match(/^([①②③④⑤⑥⑦⑧⑨⑩])\\s*(.+)$/);
        if(numItem){
            html += `<div class='ai-item'><span class='ai-bullet'>${esc(numItem[1])}</span><span>${aiInline(numItem[2])}</span></div>`;
        }else if(circleItem){
            html += `<div class='ai-item'><span class='ai-bullet'>${esc(circleItem[1])}</span><span>${aiInline(circleItem[2])}</span></div>`;
        }else{
            html += `<div class='ai-line'>${aiInline(line)}</div>`;
        }
    });
    return html;
}
// 把 AI 返回结果按顶层章节（一、/二、/三、…）拆成 Tab 卡片；无章节时整体一个 Tab
function renderAiTabs(text){
    const raw = String(text==null?"":text).trim();
    if(!raw) return "<div class='muted'>（无返回）</div>";
    const lines = raw.split(/\\r?\\n/).map(l=>l.trim()).filter(l=>l.length>0);
    if(!lines.length) return "<div class='muted'>（无返回）</div>";
    const tabs = [];
    let cur = null;
    lines.forEach(line=>{
        const m = line.match(/^(第?[一二三四五六七八九十百]+)[、.．:：)）]\\s*(.+)$/);
        if(m){
            cur = {title: (m[1]+" "+m[2]).trim(), lines: []};
            tabs.push(cur);
        }else{
            if(!cur){ cur = {title: "AI 点评", lines: []}; tabs.push(cur); }
            cur.lines.push(line);
        }
    });
    if(!tabs.length){ tabs.push({title: "AI 点评", lines: lines}); }
    let html = "<div class='ai-tabs'>";
    tabs.forEach((t,i)=>{
        html += `<button class='ai-tab${i===0?" on":""}' onclick='switchAiTab(${i})'>${esc(t.title.replace(/\\*\\*/g,""))}</button>`;
    });
    html += "</div>";
    tabs.forEach((t,i)=>{
        html += `<div class='ai-tab-pane${i===0?" on":""}'>` + renderAiTabLines(t.lines) + "</div>";
    });
    return html;
}
function switchAiTab(i){
    const tabs = document.querySelectorAll(".ai-tab");
    const panes = document.querySelectorAll(".ai-tab-pane");
    tabs.forEach((b,j)=>b.classList.toggle("on", j===i));
    panes.forEach((p,j)=>p.classList.toggle("on", j===i));
}
function renderAiText(text){
    const raw = String(text==null?"":text).trim();
    if(!raw) return "<div class='muted'>（无返回）</div>";
    const lines = raw.split(/\\r?\\n/).map(l=>l.trim()).filter(l=>l.length>0);
    if(!lines.length) return "<div class='muted'>（无返回）</div>";
    let html = "";
    lines.forEach(line=>{
        // 分节标题：一、/二、/【标题】/ **短加粗行**
        const zhSec = line.match(/^(第?[一二三四五六七八九十百]+)[、.．:：)）]\\s*(.+)$/);
        const cn = line.match(/^【([^】]+)】\\s*(.+)$/);
        const bold = line.match(/^\\*\\*(.+?)\\*\\*$/);
        // 列表条目：1. 2、① ②
        const numItem = line.match(/^(\\d+)[、.．:：)）]\\s*(.+)$/);
        const circleItem = line.match(/^([①②③④⑤⑥⑦⑧⑨⑩])\\s*(.+)$/);
        if(zhSec){
            html += `<div class='ai-sec'><span class='ai-sec-no'>${esc(zhSec[1])}</span><span class='ai-sec-body'>${aiInline(zhSec[2])}</span></div>`;
        }else if(cn){
            html += `<div class='ai-sec'><span class='ai-sec-no'>${esc(cn[1])}</span><span class='ai-sec-body'>${aiInline(cn[2])}</span></div>`;
        }else if(bold){
            html += `<div class='ai-sec'><span class='ai-sec-body'>${aiInline(bold[1])}</span></div>`;
        }else if(numItem){
            html += `<div class='ai-item'><span class='ai-bullet'>${esc(numItem[1])}</span><span>${aiInline(numItem[2])}</span></div>`;
        }else if(circleItem){
            html += `<div class='ai-item'><span class='ai-bullet'>${esc(circleItem[1])}</span><span>${aiInline(circleItem[2])}</span></div>`;
        }else{
            html += `<div class='ai-line'>${aiInline(line)}</div>`;
        }
    });
    return html;
}

// ===== 导出所选素材全量数据为 HTML 文件 =====
async function exportMaterialHtml(){
    const mid = document.getElementById("materialSelect").value;
    if(!mid){ alert("请先在素材下拉框中选择一个素材"); return; }
    const aid = document.getElementById("matAdvertiser").value;
    const btn = document.getElementById("exportBtn");
    if(btn.disabled) return;
    setBusy(btn, true, "导出中…");
    try{
        const resp = await fetch("/api/material_export?material_id=" + encodeURIComponent(mid) + (aid ? ("&advertiser_id="+encodeURIComponent(aid)) : ""));
        const res = await resp.json();
        if(!res.success){ alert("导出失败：" + (res.error||"")); return; }
        const matName = String((res.material&&res.material.name) || res.summary.name || "").trim();
        const safe = matName.replace(/[\\/:*?"<>|]/g, "_").replace(/\\s+$/g, "").slice(0, 40);
        const fileName = "素材_" + (safe ? safe + "_" : "") + mid + "_全量数据.html";
        const html = buildExportHtml(res);
        const blob = new Blob(["\ufeff" + html], {type:"text/html;charset=utf-8"});
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = fileName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        alert("已导出：" + fileName + "（" + (res.plans||[]).length + " 个关联计划）");
    }catch(e){
        alert("导出失败：" + e);
    }finally{
        setBusy(btn, false);
    }
}

function buildExportHtml(res){
    const s = res.summary||{}, daily = res.daily||[], lib = res.material||{},
          plans = res.plans||[], products = res.products||[], ai = res.ai||"(无返回)";
    const matName = (lib.name || s.name || res.material_id).toString();
    const kv = (label, val) => `<tr><td class="k">${esc(label)}</td><td>${esc(val==null?"":val)}</td></tr>`;
    // 汇总指标（中文名 → 值）
    const summaryRows = [
        ["素材名称", s.name], ["素材类型", s.type + (s.material_types && s.material_types.length ? "（" + s.material_types.join("/") + "）" : "")], ["消耗（元）", s.消耗],
        ["展示", s.展示], ["点击", s.点击], ["点击率 CTR", s.点击率 + "%"],
        ["转化率 CVR", s.转化率 + "%"], ["成交单数", s.成交单数], ["成交金额（元）", s.成交金额],
        ["支付 ROI", s.支付ROI], ["投放天数", s.active_days], ["投放区间", (s.first_day||"") + " ~ " + (s.last_day||"")],
        ["素材状态", s.material_status], ["审核状态", s.audit_status],
        ["关联商品数", (s.products && s.products.length) || "—"],
        ["峰值日", s.peak_day + (s.peak_cost!=null ? "（" + s.peak_cost + " 元）" : "")],
        ["近 7 天消耗（元）", s.recent7_cost], ["前 7 天消耗（元）", s.prev7_cost], ["近 7 天趋势", s.trend],
    ].filter(r => r[1] != null && r[1] !== "" && r[1] !== "undefined");
    // 素材库信息（已知字段转中文，其余原样）
    const libLabels = {id:"素材ID", name:"素材名称", kind:"素材类型", url:"播放地址",
                       poster_url:"封面地址", image_url:"图片地址", note:"备注", type:"素材类型"};
    const libRows = Object.keys(lib).filter(k => k !== "id")
        .map(k => [libLabels[k] || k, lib[k]]);
    const planRows = plans.map(p => `<tr><td>${esc(p.plan_id)}</td><td>${esc(p.plan_name)}</td><td>${esc(p.status)}</td><td>${esc(p.product_id||"—")}</td></tr>`).join("");
    const productRows = products.map(p => `<tr><td>${esc(p.product_id)}</td></tr>`).join("");
    const dailyRows = daily.map(d => `<tr><td>${esc(d.date)}</td><td>${esc(d.cost)}</td><td>${esc(d.show)}</td><td>${esc(d.click)}</td><td>${esc(d.orders)}</td><td>${esc(d.gmv)}</td></tr>`).join("");
    // 全部报表指标（25项，卡片网格样式）
    const allMetricCards = (s.metrics_all || []).map(m =>
        `<div class="mcard"><div class="mlab">${esc(m.cn)}</div><div class="mval">${esc(m.value)}</div></div>`).join("");
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>素材 ${esc(res.material_id)} 全量投放数据 - ${esc(matName)}</title>
<style>
body{font-family:"Microsoft YaHei",PingFang SC,sans-serif;margin:24px auto;max-width:960px;padding:0 16px;color:#1f2329;background:#fff}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px;padding-left:8px;border-left:3px solid #3370ff}
.meta{color:#86909c;font-size:13px;margin-bottom:20px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #e5e6eb;padding:6px 10px;text-align:left;word-break:break-all}
th{background:#f7f8fa;font-weight:600;white-space:nowrap}
td.k{background:#fafbfc;color:#4e5969;white-space:nowrap;width:160px}
tr:nth-child(even) td{background:#fcfcfd}
.ai{background:linear-gradient(180deg,#fbfcff,#f4f8ff);border:1px solid #dbe4ff;border-radius:8px;padding:14px 16px;font-size:13px;line-height:1.9}
.ai-sec{margin:8px 0 3px;display:flex;align-items:flex-start;gap:7px}
.ai-sec-no{flex:none;min-width:20px;height:20px;line-height:20px;text-align:center;background:linear-gradient(135deg,#4aa3ff,#1f6feb);color:#fff;border-radius:5px;font-size:12px;font-weight:700;margin-top:2px}
.ai-sec-body{font-weight:700;color:#1f6feb}
.ai-item{display:flex;gap:6px;margin:4px 0 4px 2px}
.ai-bullet{flex:none;color:#3370ff;font-weight:700}
.ai-line{margin:2px 0}
.ai-line b,.ai-item b{color:#d25f00}
.note{color:#86909c;font-size:12px}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:8px}
.mcard{background:#f7f8fa;border:1px solid #eceff3;border-radius:12px;padding:10px 12px}
.mlab{font-size:12px;color:#8a94a6;margin-bottom:4px}
.mval{font-size:17px;font-weight:700;color:#2d5bff;word-break:break-all}
@media print{body{margin:0}}
</style>
</head>
<body>
<h1>素材 ${esc(res.material_id)} 全量投放数据</h1>
<div class="meta">素材名称：${esc(matName)}　|　广告主ID：${esc(res.advertiser_id||"—")}　|　导出时间：${esc(new Date().toLocaleString("zh-CN"))}</div>

<h2>一、素材基本信息</h2>
<table>${libRows.length ? libRows.map(r=>kv(r[0],r[1])).join("") : kv("素材ID", res.material_id)}</table>

<h2>二、汇总指标</h2>
<table>${summaryRows.map(r=>kv(r[0],r[1])).join("")}</table>

<h2>三、全部报表指标（${(s.metrics_all||[]).length} 项）</h2>
${allMetricCards ? `<div class="mgrid">${allMetricCards}</div>` : `<div class="note">无</div>`}

<h2>四、逐日明细（${daily.length} 天）</h2>
${daily.length ? `<table><tr><th>日期</th><th>消耗</th><th>展示</th><th>点击</th><th>成交单</th><th>成交金额</th></tr>${dailyRows}</table>` : `<div class="note">无逐日数据</div>`}

<h2>五、关联计划（${plans.length}）</h2>
${plans.length ? `<table><tr><th>计划ID</th><th>计划名称</th><th>状态</th><th>商品ID</th></tr>${planRows}</table>` : `<div class="note">该素材当前未关联在投计划（或账户暂无在投全域计划）</div>`}

<h2>六、关联商品（${products.length}）</h2>
${products.length ? `<table><tr><th>商品ID</th></tr>${productRows}</table>` : `<div class="note">无关联商品</div>`}

<h2>七、AI 点评与修改建议</h2>
<div class="ai">${renderAiText(ai)}</div>
</body>
</html>`;
}

// ===== 标签设置：素材标签增删改查 =====
let tagRows = [];          // 当前标签列表缓存
let tagEditId = null;      // 当前编辑中的标签主键；null 表示新增模式

function openTags(){
    document.getElementById("tagModal").classList.add("show");
    loadTags();
}
function closeTags(){
    document.getElementById("tagModal").classList.remove("show");
}
function resetTagForm(){
    tagEditId = null;
    document.getElementById("tagName").value = "";
    document.getElementById("tagColor").value = "#1f6feb";
    document.getElementById("tagSaveBtn").innerText = "新增";
    document.getElementById("tagCancelBtn").style.display = "none";
}
async function loadTags(){
    const tbody = document.getElementById("tagTbody");
    tbody.innerHTML = "<tr><td colspan='3' class='muted'>加载中…</td></tr>";
    try{
        const res = await (await fetch("/api/tags")).json();
        if(!res.success){
            tbody.innerHTML = "<tr><td colspan='3'>加载失败：" + esc(res.error||"") + "</td></tr>";
            return;
        }
        tagRows = res.data || [];
        if(!tagRows.length){
            tbody.innerHTML = "<tr><td colspan='3' class='muted'>暂无标签，输入名称后点击「新增」</td></tr>";
            return;
        }
        tbody.innerHTML = tagRows.map(t=>
            `<tr><td><span style="display:inline-block;width:18px;height:18px;border-radius:5px;background:${esc(t.color)};vertical-align:middle;border:1px solid #e6ebf2"></span></td>`+
            `<td>${esc(t.name)}</td>`+
            `<td><button class='op' data-act='tag-edit' data-id='${t.id}'>编辑</button> `+
            `<button class='op del' data-act='tag-del' data-id='${t.id}'>删除</button></td></tr>`
        ).join("");
    }catch(e){
        tbody.innerHTML = "<tr><td colspan='3'>加载失败</td></tr>";
    }
}
async function saveTag(){
    const name = document.getElementById("tagName").value.trim();
    if(!name){ alert("请输入标签名称"); return; }
    const payload = {name, color: document.getElementById("tagColor").value};
    if(tagEditId != null) payload.id = tagEditId;
    try{
        const res = await (await fetch("/api/tags", {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify(payload)
        })).json();
        if(!res.success){ alert("保存失败：" + (res.error||"")); return; }
        resetTagForm();
        loadTags();
    }catch(e){ alert("保存失败：" + e); }
}

// ===== 设置：广告主账户 =====
let editingAccountId = null;  // 当前编辑中的记录主键；null 表示新增模式

function openSettings(){
    document.getElementById("settingsModal").classList.add("show");
    loadAccounts();
}
function closeSettings(){
    document.getElementById("settingsModal").classList.remove("show");
    newAccount();
}
// HTML 属性值安全转义（用于 data-* 属性，兼容名称含引号/尖括号的情况）
function escAttr(s){
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
        .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
async function loadAccounts(){
    const tb = document.getElementById("accTbody");
    tb.innerHTML = "<tr><td colspan=3>加载中…</td></tr>";
    try{
        const res = await (await fetch("/api/advertiser_accounts")).json();
        if(!res.success){ tb.innerHTML = "<tr><td colspan=3>加载失败</td></tr>"; return; }
        if(!res.data.length){ tb.innerHTML = "<tr><td colspan=3 class='muted'>暂无账户，请在上方添加</td></tr>"; return; }
        tb.innerHTML = res.data.map(a=>
            `<tr><td>${esc(a.advertiser_id)}</td><td>${esc(a.name)}</td>`+
            `<td><button class='op' data-act='edit' data-id='${a.id}' data-aid="${escAttr(a.advertiser_id)}" data-name="${escAttr(a.name)}">改</button> `+
            `<button class='op del' data-act='del' data-id='${a.id}'>删</button></td></tr>`
        ).join("");
    }catch(e){ tb.innerHTML = "<tr><td colspan=3>加载失败</td></tr>"; }
}
// 事件委托：统一处理账户列表的 改/删（避免内联 onclick 引号转义问题）
document.addEventListener("click", function(e){
    const btn = e.target.closest("button[data-act]");
    if(!btn) return;
    const act = btn.dataset.act;
    const id = Number(btn.dataset.id);
    if(act === "edit"){
        editAccount(id, btn.dataset.aid || "", btn.dataset.name || "");
    }else if(act === "del"){
        delAccount(id);
    }else if(act === "tag-edit"){
        tagEdit(id);
    }else if(act === "tag-del"){
        tagDelete(id);
    }
});
function tagEdit(id){
    const row = tagRows.find(t=>t.id===id);
    if(!row) return;
    tagEditId = id;
    document.getElementById("tagName").value = row.name;
    document.getElementById("tagColor").value = row.color;
    document.getElementById("tagSaveBtn").innerText = "保存修改";
    document.getElementById("tagCancelBtn").style.display = "";
}
async function tagDelete(id){
    if(!confirm("确认删除该标签？")) return;
    try{
        const res = await (await fetch("/api/tags/"+id, {method:"DELETE"})).json();
        if(!res.success){ alert("删除失败：" + (res.error||"")); return; }
        loadTags();
    }catch(e){ alert("删除失败：" + e); }
}
function newAccount(){
    editingAccountId = null;
    const idBox = document.getElementById("accId");
    idBox.value = "";
    idBox.readOnly = false;
    idBox.placeholder = "广告主ID（编辑时锁定）";
    document.getElementById("accName").value = "";
    idBox.focus();
}
function editAccount(id, advertiser_id, name){
    editingAccountId = id;
    const idBox = document.getElementById("accId");
    idBox.value = advertiser_id;
    idBox.readOnly = true;   // 编辑时锁定广告主ID，只能改名称，避免被误改导致新增重复记录
    idBox.placeholder = "广告主ID（编辑中，锁定）";
    document.getElementById("accName").value = name;
    document.getElementById("accName").focus();
}
async function saveAccount(){
    const advertiser_id = document.getElementById("accId").value.trim();
    const name = document.getElementById("accName").value.trim();
    if(!advertiser_id){ alert("请填广告主ID"); return; }
    const payload = {advertiser_id, name};
    if(editingAccountId != null) payload.id = editingAccountId;
    try{
        const res = await (await fetch("/api/advertiser_accounts",{
            method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify(payload)
        })).json();
        if(res.success){
            newAccount();
            loadAccounts();
            loadAdvertiserSelects();
        }else{ alert("保存失败："+(res.error||"")); }
    }catch(e){ alert("请求失败："+e); }
}
async function delAccount(id){
    if(!confirm("确认删除该账户？")) return;
    try{
        await fetch("/api/advertiser_accounts/"+id, {method:"DELETE"});
        if(editingAccountId === id) newAccount();
        loadAccounts();
        loadAdvertiserSelects();
    }catch(e){ alert("删除失败："+e); }
}
