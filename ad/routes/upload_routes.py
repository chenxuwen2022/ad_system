import os
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from ad.file_service import save_upload_file
from ad.models import UploadResp

router = APIRouter()

# 本地素材库目录：遍历该目录下的图片/视频用于预览与投放
# 可用环境变量 LOCAL_MATERIAL_DIR 覆盖（服务器部署时指向服务器上的素材目录）
LOCAL_MATERIAL_DIR = os.environ.get("LOCAL_MATERIAL_DIR", r"C:\test素材")
# 上传素材库目录：页面上传的素材单独存放，与本地素材库区分
UPLOAD_MATERIAL_DIR = os.environ.get(
    "UPLOAD_MATERIAL_DIR", r"C:\Users\33082\PycharmProjects\ad_system\upload_materials")
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


HTML_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>商城 · 千川素材投放台</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
            background: #f3f5f8; color: #1f2733; line-height: 1.6; padding: 24px;
        }
        .wrap { max-width: 980px; margin: 0 auto; }
        header {
            position:relative;
            background: linear-gradient(135deg, #1f6feb, #0aa5a5);
            color: #fff; border-radius: 14px; padding: 22px 28px; margin-bottom: 20px;
            box-shadow: 0 6px 18px rgba(31,111,235,.18);
        }
        header h1 { font-size: 22px; font-weight: 700; }
        header p { font-size: 13px; opacity: .9; margin-top: 4px; }
        .card {
            background: #fff; border-radius: 14px; padding: 20px 24px; margin-bottom: 18px;
            box-shadow: 0 2px 10px rgba(20,40,80,.06); border: 1px solid #eaeef4;
        }
        .card h3 {
            font-size: 16px; margin-bottom: 14px; padding-left: 10px;
            border-left: 4px solid #1f6feb;
        }
        .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
        input[type=text], select {
            padding: 9px 12px; border: 1px solid #d4dbe5; border-radius: 8px;
            font-size: 14px; background: #fff; outline: none; transition: .15s;
        }
        input[type=text]:focus, select:focus { border-color: #1f6feb; box-shadow: 0 0 0 3px rgba(31,111,235,.12); }
        input[type=file] { font-size: 14px; }
        button {
            padding: 9px 18px; border: none; border-radius: 8px; font-size: 14px;
            background: #1f6feb; color: #fff; cursor: pointer; transition: .15s; font-weight: 500;
        }
        button:hover { background: #1559c4; }
        button:disabled { background: #9bb8e6; cursor: not-allowed; }
        button.ghost { background: #eef2f8; color: #33486b; }
        button.ghost:hover { background: #dfe7f2; }
        pre {
            background: #f6f8fb; border-radius: 8px; padding: 12px 14px; margin-top: 12px;
            font-size: 12.5px; white-space: pre-wrap; word-break: break-all; color: #33486b;
            max-height: 260px; overflow: auto;
        }
        .kpi { display:grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin:12px 0; }
        .kpi .cell { background:#f7f8fa; border:1px solid #eceff3; border-radius:12px; padding:12px 14px; }
        .kpi .lab { font-size:12px; color:#8a94a6; margin-bottom:4px; }
        .kpi .val { font-size:21px; font-weight:700; color:#2d5bff; margin-top:2px; }
        .kpi.kpi-all { grid-template-columns: repeat(auto-fill,minmax(165px,1fr)); gap:8px; }
        .kpi.kpi-all .cell { padding:10px 12px; }
        .kpi.kpi-all .val { font-size:17px; word-break:break-all; }
        table.data { border-collapse: collapse; width:100%; font-size:13px; margin-top:8px; }
        table.data th, table.data td { border:1px solid #e6ebf2; padding:7px 10px; text-align:center; }
        table.data th { background:#f0f4fa; color:#44566f; font-weight:600; }
        .barwrap { display:flex; align-items:flex-end; height:130px; border-bottom:2px solid #d4dbe5; gap:2px; margin-top:8px; }
        .bar { flex:1; background:linear-gradient(180deg,#4aa3ff,#1f6feb); border-radius:3px 3px 0 0; min-width:2px; }
        .preview-box img, .preview-box video { max-width:300px; max-height:480px; border-radius:10px; border:1px solid #e6ebf2; display:block; }
        .upload-card {
            border:2px dashed #ccd6e4; border-radius:10px; background:#fafbfd;
            display:flex; align-items:center; justify-content:center; min-height:212px;
            cursor:pointer; transition:.15s; user-select:none; text-align:center;
        }
        .upload-card:hover { border-color:#1f6feb; color:#1f6feb; background:#f4f8ff; }
        .upload-card .uc-plus { font-size:36px; line-height:1; color:#aab6c8; }
        .upload-card:hover .uc-plus { color:#1f6feb; }
        .upload-card .uc-title { margin-top:10px; font-size:14px; font-weight:600; color:#44566f; }
        .upload-card .uc-sub { font-size:12px; color:#9aa7ba; margin-top:4px; line-height:1.6; }
        .upload-card .uc-tip { margin-top:10px; font-size:12px; color:#1f6feb; display:none; }
        .lib-tabs { display:flex; gap:10px; }
        .lib-tab { padding:9px 22px; border-radius:9px; border:1px solid #d4dbe5;
            background:#f4f6fa; color:#44566f; font-size:14px; font-weight:600;
            cursor:pointer; transition:.15s; }
        .lib-tab:hover { border-color:#1f6feb; color:#1f6feb; }
        .lib-tab.on { background:linear-gradient(120deg,#1f6feb,#3b82f6); color:#fff;
            border-color:transparent; box-shadow:0 3px 10px rgba(31,111,235,.25); }
        .upload-zone { margin-top:14px; border:2px dashed #ccd6e4; border-radius:12px;
            background:#fafbfd; padding:26px 20px; text-align:center; cursor:pointer; transition:.15s; }
        .upload-zone:hover { border-color:#1f6feb; background:#f4f8ff; }
        .upload-zone .uc-plus { font-size:38px; line-height:1; color:#aab6c8; }
        .upload-zone:hover .uc-plus { color:#1f6feb; }
        .upload-zone .uc-title { margin-top:10px; font-size:15px; font-weight:600; color:#44566f; }
        .upload-zone .uc-sub { font-size:12.5px; color:#9aa7ba; margin-top:6px; line-height:1.6; }
        .upload-zone .uc-tip { margin-top:10px; font-size:13px; color:#1f6feb; display:none; }
        .ai-box { background:linear-gradient(180deg,#fbfcff 0%,#f4f8ff 100%); border:1px solid #dbe4ff; border-radius:12px; padding:16px 18px; margin-top:10px; font-size:14px; line-height:1.9; color:#1f2329; box-shadow:0 1px 4px rgba(31,111,235,.06); }
        .ai-head { display:flex; align-items:center; gap:8px; margin-top:18px; font-size:14px; font-weight:700; color:#1f2329; }
        .ai-head::before { content:""; width:4px; height:16px; border-radius:2px; background:linear-gradient(180deg,#4aa3ff,#1f6feb); }
        .ai-sec { margin:10px 0 4px; display:flex; align-items:flex-start; gap:8px; }
        .ai-sec-no { flex:none; min-width:22px; height:22px; line-height:22px; text-align:center; background:linear-gradient(135deg,#4aa3ff,#1f6feb); color:#fff; border-radius:6px; font-size:13px; font-weight:700; margin-top:2px; box-shadow:0 1px 3px rgba(31,111,235,.25); }
        .ai-sec-body { font-weight:700; color:#1f6feb; }
        .ai-item { display:flex; gap:8px; margin:5px 0 5px 4px; }
        .ai-bullet { flex:none; color:#3370ff; font-weight:700; }
        .ai-line { margin:3px 0; }
        .ai-line b, .ai-item b { color:#d25f00; }
        .ai-tabs { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
        .ai-tab { padding:7px 16px; border-radius:8px; border:1px solid #d4dbe5;
            background:#f4f6fa; color:#44566f; font-size:13px; font-weight:600;
            cursor:pointer; transition:.15s; }
        .ai-tab:hover { border-color:#1f6feb; color:#1f6feb; }
        .ai-tab.on { background:linear-gradient(120deg,#1f6feb,#3b82f6); color:#fff;
            border-color:transparent; box-shadow:0 3px 10px rgba(31,111,235,.25); }
        .ai-tab-pane { display:none; }
        .ai-tab-pane.on { display:block; }
        .viz-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }
        .viz-stage { padding:5px 14px; border-radius:14px; font-size:13px; font-weight:700; color:#fff; }
        .viz-stage.cold { background:linear-gradient(120deg,#8b5cf6,#a78bfa); }
        .viz-stage.up { background:linear-gradient(120deg,#16a34a,#22c55e); }
        .viz-stage.stable { background:linear-gradient(120deg,#1f6feb,#3b82f6); }
        .viz-stage.down { background:linear-gradient(120deg,#dc2626,#f87171); }
        .viz-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px 18px; margin-bottom:14px;
            background:#fff; border:1px solid #e7edf6; border-radius:10px; padding:12px 14px; }
        .viz-bar { display:flex; align-items:center; gap:8px; min-width:0; }
        .viz-lab { flex:none; width:64px; font-size:12px; color:#55637a; text-align:right; }
        .viz-track { flex:1; height:8px; border-radius:4px; background:#eef2f7; overflow:hidden; }
        .viz-fill { height:100%; border-radius:4px; transition:width .4s; }
        .viz-grade { flex:none; width:34px; font-size:12px; font-weight:700; }
        .viz-val { flex:none; font-size:11px; color:#8a97ab; }
        .viz-note { font-size:12px; color:#8a97ab; background:#fff; border:1px solid #e7edf6;
            border-radius:8px; padding:6px 10px; margin-bottom:12px; }
        .viz-note b { color:#1f6feb; }
        .viz-sec { display:flex; align-items:center; gap:8px; margin:12px 0 8px; }
        .viz-sec-tag { font-size:13px; font-weight:700; color:#fff; padding:4px 12px; border-radius:8px;
            background:linear-gradient(120deg,#1f6feb,#3b82f6); box-shadow:0 1px 3px rgba(31,111,235,.25); }
        .viz-chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:4px; }
        .viz-chip { background:#eef4ff; border:1px solid #d5e2ff; color:#2b5bff; font-size:12.5px;
            padding:6px 12px; border-radius:14px; line-height:1.5; }
        .viz-cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px; }
        .viz-card { background:#fff; border:1px solid #e7edf6; border-radius:10px; padding:10px 12px;
            display:flex; gap:10px; align-items:flex-start; box-shadow:0 1px 2px rgba(31,111,235,.05); }
        .viz-no { flex:none; min-width:22px; height:22px; line-height:22px; text-align:center;
            background:linear-gradient(135deg,#4aa3ff,#1f6feb); color:#fff; border-radius:6px;
            font-size:12.5px; font-weight:700; margin-top:1px; }
        .viz-txt { font-size:13px; color:#1f2329; line-height:1.7; }
        .viz-txt b { color:#d25f00; }
        .muted { color:#8a97ab; font-size:13px; }
        .spin { color:#1f6feb; }
        .settings-btn {
            background:rgba(255,255,255,.2); color:#fff; border:1px solid rgba(255,255,255,.5);
            border-radius:8px; padding:8px 16px; font-size:14px; cursor:pointer;
        }
        .settings-btn:hover { background:rgba(255,255,255,.32); }
        .modal-mask {
            display:none; position:fixed; inset:0; background:rgba(0,0,0,.45);
            z-index:100; align-items:center; justify-content:center;
        }
        .modal-mask.show { display:flex; }
        .modal {
            background:#fff; border-radius:14px; width:560px; max-width:92vw;
            max-height:86vh; overflow:auto; padding:24px;
        }
        .modal h3 { font-size:17px; margin-bottom:14px; }
        .modal table { width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }
        .modal th, .modal td { border:1px solid #e6ebf2; padding:7px 9px; text-align:center; }
        .modal th { background:#f0f4fa; }
        .modal .row input { margin-right:8px; }
        .modal .op { padding:3px 10px; font-size:12px; }
        .modal .op.del { background:#e5484d; }
        /* ===== 投放弹窗（选择商品并投放）美化 ===== */
        .modal-launch { width:960px; max-width:96vw; padding:0; border-radius:18px; overflow:hidden;
            box-shadow:0 24px 60px rgba(15,40,90,.28);
            height:98vh; max-height:98vh; display:flex; flex-direction:column; }
        .lm-head { display:flex; align-items:center; gap:12px; padding:16px 22px;
            background:linear-gradient(120deg,#1559c4,#3b82f6); }
        .lm-title { font-size:17px; font-weight:700; color:#fff; white-space:nowrap; }
        .lm-file { flex:1; min-width:0; font-size:13px; color:rgba(255,255,255,.92);
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
            background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.25);
            padding:5px 12px; border-radius:8px; }
        .lm-close { flex:none; width:32px; height:32px; border:none; border-radius:9px;
            background:rgba(255,255,255,.16); color:#fff; font-size:17px; line-height:1;
            cursor:pointer; padding:0; }
        .lm-close:hover { background:rgba(255,255,255,.32); }
        .lm-steps { display:flex; gap:8px; padding:14px 22px 0; }
        .lm-step { font-size:12px; color:#8a97ab; padding:5px 13px; border-radius:20px;
            background:#f0f4fa; font-weight:500; }
        .lm-step.on { background:#e3efff; color:#1f6feb; font-weight:700; }
        .lm-body { padding:16px 22px 22px; display:flex; flex-direction:column; gap:14px;
            flex:1; min-height:0; overflow:hidden; }
        .lm-fields { display:flex; flex-direction:column; gap:14px; overflow:auto; min-height:0;
            flex:0 1 55%; }
        .lm-bottom { flex:1; min-height:0; display:flex; flex-direction:column; gap:12px; }
        .lm-actions { flex:none; }
        .lm-field { background:#f8fafd; border:1px solid #edf1f7; border-radius:14px; padding:14px 16px; }
        .lm-label { display:flex; align-items:center; gap:8px; font-size:13.5px; font-weight:700;
            color:#2b3a52; margin-bottom:10px; }
        .lm-label::before { content:""; flex:none; width:4px; height:15px; border-radius:2px;
            background:linear-gradient(180deg,#4aa3ff,#1f6feb); }
        .lm-hint { font-size:12px; color:#9aa7ba; font-weight:400; }
        .lm-line { display:flex; gap:10px; align-items:center; }
        .lm-line select { flex:1; min-width:0; padding:10px 12px; }
        .lm-search { width:200px; }
        .lm-refresh { flex:none; white-space:nowrap; }
        .lm-summary { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:10px; }
        .lm-summary .k { background:#fff; border:1px solid #e7edf6; border-radius:10px; padding:8px 10px; }
        .lm-summary .lab { font-size:11px; color:#8a97ab; }
        .lm-summary .val { font-size:15px; font-weight:700; color:#2b3a52; margin-top:1px; }
        .lm-summary .val.hl { color:#1f6feb; }
        .lm-actions { display:flex; align-items:center; justify-content:space-between; gap:14px; }
        .lm-test { display:flex; align-items:center; gap:7px; font-size:13px; color:#55637a;
            cursor:pointer; background:#f0f4fa; border:1px solid #e4ebf4; padding:8px 14px; border-radius:10px; }
        .lm-test input { margin:0; accent-color:#1f6feb; }
        .lm-launch { padding:11px 26px; font-size:15px; border-radius:10px;
            background:linear-gradient(120deg,#1f6feb,#3b82f6); box-shadow:0 4px 12px rgba(31,111,235,.3); }
        .lm-launch:hover { background:linear-gradient(120deg,#1559c4,#2f74e8); }
        .lm-result { border-radius:12px; padding:16px 18px; margin-top:2px; font-size:16px;
            white-space:pre-wrap; word-break:break-all; overflow:auto; flex:1; min-height:0;
            font-family:Consolas,Menlo,monospace; line-height:2; }
        .lm-result.ok { background:#f0faf3; border:1px solid #c9ecd4; color:#1a7f37; }
        .lm-result.err { background:#fdf2f2; border:1px solid #f3cccc; color:#c0392b; }
        .lm-tags { display:flex; flex-wrap:wrap; gap:8px; }
        .lm-tag { display:inline-flex; align-items:center; gap:6px; padding:7px 14px; border-radius:20px;
            border:1px solid #dde5f0; background:#fff; font-size:13px; color:#44566f; cursor:pointer;
            user-select:none; transition:.15s; }
        .lm-tag .dot { width:10px; height:10px; border-radius:50%; flex:none; }
        .lm-tag:hover { border-color:#1f6feb; }
        .lm-tag.on { background:#e8f1ff; border-color:#1f6feb; color:#1f6feb; font-weight:600; }
        .lm-tag.on .dot { box-shadow:0 0 0 2px #fff inset; }
        .lm-tags-empty { font-size:12.5px; color:#9aa7ba; }
        .mat-card { position:relative; }
        .mat-check { position:absolute; top:8px; left:8px; width:22px; height:22px; border-radius:50%;
            background:rgba(255,255,255,.92); border:2px solid #c2cad6; display:flex; align-items:center;
            justify-content:center; font-size:13px; color:#fff; cursor:pointer; z-index:2; transition:.15s;
            user-select:none; line-height:1; }
        .mat-check:hover { border-color:#1f6feb; }
        .mat-check.on { background:#1f6feb; border-color:#1f6feb; }
        .mat-card.sel { border-color:#1f6feb; box-shadow:0 0 0 2px rgba(31,111,235,.15); }
        .launch-badge { position:absolute; top:36px; right:8px; z-index:2; font-size:11px;
            padding:2px 8px; border-radius:10px; background:rgba(255,255,255,.94);
            border:1px solid #e0e6ef; color:#8a97ab; cursor:default; line-height:1.5; }
        .launch-badge.ok { color:#1a7f37; border-color:#c9ecd4; background:rgba(240,250,243,.94); }
        .launch-badge.test { color:#b26a00; border-color:#f2d9a6; background:rgba(255,248,235,.94); }
        .launch-badge.fail { color:#c0392b; border-color:#f3cccc; background:rgba(253,242,242,.94); }
        .lib-tabs { display:flex; align-items:center; gap:10px; }
        .batch-btn { margin-left:auto; }
        .lm-batch-summary { font-size:15px; font-weight:700; margin-bottom:10px; }
        .lm-batch-item { border:1px solid #e7edf6; border-radius:10px; padding:10px 14px; margin-bottom:8px; background:#fff; }
        .lm-batch-item.ok { border-color:#c9ecd4; }
        .lm-batch-item.err { border-color:#f3cccc; background:#fdf8f8; }
        .lm-batch-item pre { margin:6px 0 0; font-size:12px; white-space:pre-wrap; word-break:break-all; }
    </style>
</head>
<body>
<div class="wrap">
    <header>
        <h1>数语深流 · 巨量千川素材投放台</h1>
        <p>本地素材库 · 数据回流（AI）</p>
        <div style="position:absolute;top:22px;right:28px;display:flex;gap:10px">
            <button class="settings-btn" onclick="openTags()">标签设置</button>
            <button class="settings-btn" onclick="openSettings()">店铺设置</button>
        </div>
    </header>

    <div class="card">
        <div class="lib-tabs">
            <button class="lib-tab on" id="tabLocal" onclick="switchLib('local')">本地素材库</button>
            <button class="lib-tab" id="tabUpload" onclick="switchLib('upload')">上传素材库</button>
            <button class="ghost batch-btn" id="batchLaunchBtn" onclick="openBatchLaunchModal()" style="display:none">批量投放(0)</button>
        </div>
        <div id="libLocal">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-top:14px">
                <h3 style="margin-bottom:0">本地素材</h3>
                <button class="ghost" onclick="loadLocalMaterials()">刷新目录</button>
            </div>
            <div id="localMaterialBox" style="margin-top:10px;"><p class="muted">点击「刷新目录」加载素材列表</p></div>
        </div>
        <div id="libUpload" style="display:none">
            <div class="upload-zone" id="uploadZone">
                <div class="uc-plus">＋</div>
                <div class="uc-title">上传素材</div>
                <div class="uc-sub">点击或拖拽文件到此处，支持图片 / 视频，可多选</div>
                <div class="uc-tip" id="uploadTip"></div>
                <input type="file" id="uploadInput" accept=".jpg,.jpeg,.png,.gif,.webp,.bmp,.mp4,.mov,.avi,.mkv,.webm" multiple style="display:none">
            </div>
            <div style="display:flex;align-items:center;justify-content:space-between;margin-top:16px">
                <h3 style="margin-bottom:0">已上传素材</h3>
                <button class="ghost" onclick="loadUploadedMaterials()">刷新</button>
            </div>
            <div id="uploadedMaterialBox" style="margin-top:10px;"><p class="muted">暂无上传素材，请在上方上传</p></div>
        </div>
    </div>

    <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between">
            <h3 style="margin-bottom:0">数据回流</h3>
            <button class="ghost" id="exportBtn" onclick="exportMaterialHtml()"
                    title="导出所选素材的全量投放数据为 HTML 文件">导出 HTML</button>
        </div>
        <div class="row">
            <label class="muted">店铺</label>
            <select id="matAdvertiser" onchange="onMatAdvertiserChange()" style="min-width:210px"></select>
            <label class="muted">商品</label>
            <select id="productSelect" onchange="onProductChange()" style="min-width:190px"><option value="">全部商品</option></select>
            <input type="text" id="materialSearch" placeholder="输入关键词筛选素材…"
                   oninput="filterMaterials()" style="width:200px">
            <select id="materialSelect" style="flex:1;min-width:240px" onchange="loadMaterialDetail()"></select>
            <button class="ghost" onclick="loadMaterialList(true)" title="从千川重新拉取最新数据（约40秒）">刷新素材</button>
        </div>
        <div id="productAgg" style="margin-top:12px"></div>
        <div id="matDetail" style="margin-top:14px;"></div>
    </div>
</div>

<div class="modal-mask" id="tagModal" onclick="if(event.target===this)closeTags()">
    <div class="modal">
        <h3>标签设置</h3>
        <div class="row">
            <input type="text" id="tagName" placeholder="标签名称（如：爆款 / 新品 / 高转化）" style="flex:1">
            <input type="color" id="tagColor" value="#1f6feb"
                   style="width:46px;height:38px;padding:2px;border:1px solid #d4dbe5;border-radius:8px;background:#fff;cursor:pointer"
                   title="选择标签颜色">
            <button onclick="saveTag()" id="tagSaveBtn">新增</button>
            <button class="ghost" onclick="resetTagForm()" id="tagCancelBtn" style="display:none">取消编辑</button>
        </div>
        <table>
            <thead><tr><th style="width:70px">颜色</th><th>标签名称</th><th style="width:150px">操作</th></tr></thead>
            <tbody id="tagTbody"></tbody>
        </table>
        <div style="text-align:right;margin-top:14px">
            <button class="ghost" onclick="closeTags()">关闭</button>
        </div>
    </div>
</div>

<div class="modal-mask" id="settingsModal" onclick="if(event.target===this)closeSettings()">
    <div class="modal">
        <h3>广告主账户设置</h3>
        <div class="row">
            <input type="text" id="accId" placeholder="广告主ID（编辑时锁定）" style="width:200px">
            <input type="text" id="accName" placeholder="名称（如：Babycare童装奥莱旗舰店）" style="flex:1">
            <button onclick="saveAccount()">保存</button>
        </div>
        <table>
            <thead><tr><th>广告主ID</th><th>名称</th><th>操作</th></tr></thead>
            <tbody id="accTbody"></tbody>
        </table>
        <div style="text-align:right;margin-top:14px">
            <button class="ghost" onclick="closeSettings()">关闭</button>
        </div>
    </div>
</div>

<div class="modal-mask" id="launchModal" onclick="if(event.target===this)closeLaunchModal()">
    <div class="modal modal-launch">
        <div class="lm-head">
            <div class="lm-title">选择商品并投放</div>
            <div class="lm-file" id="popFileName"></div>
            <button class="lm-close" onclick="closeLaunchModal()" title="关闭">×</button>
        </div>
        <div class="lm-steps">
            <div class="lm-step on">① 选择店铺</div>
            <div class="lm-step on">② 投放计划</div>
            <div class="lm-step on">③ 选择商品</div>
            <div class="lm-step">④ 执行投放</div>
        </div>
        <div class="lm-body">
            <div class="lm-fields">
                <div class="lm-field">
                    <div class="lm-label">选择店铺</div>
                    <select id="popAdvertiser" onchange="onPopAdvertiserChange()"></select>
                </div>
                <div class="lm-field">
                    <div class="lm-label">投放计划 <span class="lm-hint">素材将追加到所选计划，不会新建</span></div>
                    <div class="lm-line">
                        <input type="text" id="popPlanSearch" placeholder="输入关键词筛选计划…"
                               oninput="filterPopPlans()" class="lm-search">
                        <select id="popPlanSelect"></select>
                        <button class="ghost lm-refresh" onclick="loadPopPlans(true)" title="从千川重新拉取该店铺全部投放计划（约10-30秒）">刷新计划</button>
                    </div>
                    <div id="popPlanSummary" class="lm-summary"></div>
                </div>
                <div class="lm-field">
                    <div class="lm-label">投放商品 <span class="lm-hint">仅展示在售商品</span></div>
                    <div class="lm-line">
                        <input type="text" id="popProductSearch" placeholder="输入关键词筛选商品…"
                               oninput="filterPopProducts()" class="lm-search">
                        <select id="popProductSelect"></select>
                        <button class="ghost lm-refresh" onclick="loadPopProducts(true)" title="从千川重新拉取最新商品（约10-30秒）">刷新商品</button>
                    </div>
                </div>
                <div class="lm-field">
                    <div class="lm-label">素材标签 <span class="lm-hint">投放后为素材打标记（可多选，在右上角「标签设置」中维护）</span></div>
                    <div id="popTagBox" class="lm-tags"><span class="lm-tags-empty">加载中…</span></div>
                </div>
            </div>
            <div class="lm-bottom">
                <div class="lm-actions">
                    <label class="lm-test"
                           title="开启后不调用千川真实接口，本地模拟整条投放链路，不产生费用">
                        <input type="checkbox" id="popTestMode" style="margin:0">测试模式（本地模拟，不产生费用）
                    </label>
                    <button id="popLaunchBtn" onclick="popLaunchAd()" class="lm-launch">执行抖音投放</button>
                </div>
                <pre id="popLaunchResult" class="lm-result" style="display:none"></pre>
            </div>
        </div>
    </div>
</div>

<div class="modal-mask" id="aiCtxModal" onclick="if(event.target===this)closeAiCtxModal()">
    <div class="modal modal-launch" style="max-width:580px">
        <div class="lm-head">
            <div class="lm-title" id="aiCtxTitle">竞品链接</div>
            <button class="lm-close" onclick="closeAiCtxModal()" title="关闭">×</button>
        </div>
        <div class="lm-body" id="aiCtxBody"></div>
    </div>
</div>

<script>
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
        loadProducts();
    }else{
        document.getElementById("materialSelect").innerHTML = "<option>请先添加广告主账户</option>";
        document.getElementById("matDetail").innerHTML = "";
    }
}

function onMatAdvertiserChange(){
    document.getElementById("matDetail").innerHTML = "";
    document.getElementById("productAgg").innerHTML = "";
    loadProducts();
    loadMaterialList();
}

function setBusy(btn, busy, busyText){
    btn.disabled = busy;
    if(!btn.dataset.normalText){ btn.dataset.normalText = btn.innerText; }
    btn.innerText = busy ? busyText : btn.dataset.normalText;
}

// ===== ①·本地素材库（遍历 C:\\test素材，预览 + 弹窗投放） =====
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

// ===== AI 分析上下文：竞品链接 + 行业市场数据（保存到后台，分析时拼入提示词） =====
let aiCtxData = {links: [], market_data: "", updated: ""};
let aiCtxMode = "links";

async function loadAiCtx(){
    const aid = document.getElementById("matAdvertiser").value;
    try{
        const r = await fetch("/api/ai_context?advertiser_id=" + encodeURIComponent(aid));
        const j = await r.json();
        if(j.success){
            aiCtxData = {links: j.links || [], market_data: j.market_data || "", updated: j.updated || ""};
        }
    }catch(e){}
    updateAiCtxBadge();
}
function updateAiCtxBadge(){
    const b = document.getElementById("aiCtxBadge");
    if(!b) return;
    const n = (aiCtxData.links || []).length + (aiCtxData.market_data ? 1 : 0);
    b.textContent = n
        ? "已加载：竞品 " + (aiCtxData.links||[]).length + " 条" + (aiCtxData.market_data ? " · 行业数据 1 份" : "")
        : "未加载参考资料";
    b.style.color = n ? "#16a34a" : "#9aa7ba";
}
function openAiCtxModal(mode){
    aiCtxMode = mode;
    document.getElementById("aiCtxTitle").textContent = mode === "links" ? "竞品链接" : "行业市场数据";
    const body = document.getElementById("aiCtxBody");
    if(mode === "links"){
        body.innerHTML = `
            <div class="lm-field">
                <div class="lm-label">竞品链接 <span class="lm-hint">每行一条，供 AI 对比分析</span></div>
                <textarea id="aiCtxLinksInput" rows="7" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #e7edf6;border-radius:10px;font-size:13px;line-height:1.8;resize:vertical" placeholder="https://xxx.douyin.com/...\\nhttps://xxx.taobao.com/..."></textarea>
            </div>
            <div style="font-size:12px;color:#8a97ab" id="aiCtxLinksSaved">已保存：0 条</div>
            <div class="lm-actions">
                <button class="ghost" onclick="clearAiCtx()">清空</button>
                <button class="lm-launch" onclick="saveAiCtx()">保存</button>
            </div>`;
        document.getElementById("aiCtxLinksInput").value = (aiCtxData.links || []).join("\\n");
        document.getElementById("aiCtxLinksSaved").textContent = "已保存：" + (aiCtxData.links || []).length + " 条";
    }else{
        body.innerHTML = `
            <div class="lm-field">
                <div class="lm-label">行业市场数据 <span class="lm-hint">粘贴文本或上传 .txt/.csv 文件（\u226450KB）</span></div>
                <textarea id="aiCtxMarketInput" rows="7" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #e7edf6;border-radius:10px;font-size:13px;line-height:1.8;resize:vertical" placeholder="如：行业平均 CTR/CVR、大盘消耗趋势、竞对投放策略…"></textarea>
                <div style="margin-top:8px"><input type="file" id="aiCtxMarketFile" accept=".txt,.csv" onchange="readAiCtxFile(this)"></div>
            </div>
            <div style="font-size:12px;color:#8a97ab" id="aiCtxMarketSaved">已保存：${aiCtxData.market_data ? (aiCtxData.market_data.length + " 字") : "0 字"}${aiCtxData.updated ? "（更新于 " + aiCtxData.updated + "）" : ""}</div>
            <div class="lm-actions">
                <button class="ghost" onclick="clearAiCtx()">清空</button>
                <button class="lm-launch" onclick="saveAiCtx()">保存</button>
            </div>`;
        document.getElementById("aiCtxMarketInput").value = aiCtxData.market_data || "";
    }
    document.getElementById("aiCtxModal").classList.add("show");
}
function readAiCtxFile(inp){
    const f = inp.files && inp.files[0];
    if(!f) return;
    if(f.size > 51200){ alert("文件过大，请控制在 50KB 以内"); inp.value = ""; return; }
    const rd = new FileReader();
    rd.onload = e => { document.getElementById("aiCtxMarketInput").value = e.target.result; };
    rd.readAsText(f, "utf-8");
}
async function saveAiCtx(){
    const aid = document.getElementById("matAdvertiser").value;
    if(aiCtxMode === "links"){
        const links = document.getElementById("aiCtxLinksInput").value.split(/\\n|,|;|；/).map(s=>s.trim()).filter(s=>s);
        aiCtxData.links = links;
    }else{
        aiCtxData.market_data = document.getElementById("aiCtxMarketInput").value.trim();
    }
    try{
        const r = await fetch("/api/ai_context", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({advertiser_id: aid, links: aiCtxData.links || [], market_data: aiCtxData.market_data || ""})
        });
        const j = await r.json();
        if(j.success){
            aiCtxData.updated = new Date().toLocaleString("zh-CN");
            updateAiCtxBadge();
            alert("已保存。重新点击「深度分析」将结合参考资料输出更针对性的建议。");
            closeAiCtxModal();
        }else{
            alert("保存失败：" + (j.error || "未知错误"));
        }
    }catch(e){
        alert("保存失败：" + e);
    }
}
function clearAiCtx(){
    if(!confirm("确定清空已保存的" + (aiCtxMode === "links" ? "竞品链接" : "行业数据") + "？")) return;
    if(aiCtxMode === "links"){ aiCtxData.links = []; }
    else{ aiCtxData.market_data = ""; }
    openAiCtxModal(aiCtxMode);
}
function closeAiCtxModal(){
    document.getElementById("aiCtxModal").classList.remove("show");
}

// ===== 同一素材跨店铺分析：集合数据 + 店铺对比 =====
function fmtNum(x){ return Number(x||0).toLocaleString("zh-CN"); }
function trendTag(v){
    if(v === "上升") return "<span style='color:#16a34a;font-weight:700'>↑ 上升</span>";
    if(v === "下滑") return "<span style='color:#dc2626;font-weight:700'>↓ 下滑</span>";
    if(v === "数据不足") return "<span style='color:#9aa7ba'>—</span>";
    return "<span style='color:#1f6feb'>→ 平稳</span>";
}
async function loadMultiShop(mid, aid){
    const box = document.getElementById("multiShopBox");
    if(!box) return;
    try{
        const r = await fetch("/api/material_multi_shop?material_id=" + encodeURIComponent(mid) + (aid ? ("&advertiser_id="+encodeURIComponent(aid)) : ""));
        const j = await r.json();
        if(!j.success){ box.innerHTML = "<div class='muted' style='margin-top:12px'>跨店铺对比加载失败：" + esc(j.error||"") + "</div>"; return; }
        const shops = j.shops || [];
        let h = "<div style='margin-top:16px'>";
        // 同一素材跨店铺对比
        const okShops = shops.filter(s=>s.ok);
        if(okShops.length > 1){
            h += "<h4 style='font-size:14px;margin:14px 0 8px'>同一素材 · 各店铺对比</h4>";
            h += "<div style='max-height:320px;overflow-y:auto;border:1px solid #e7edf6;border-radius:10px'><table class='data' style='margin:0;width:100%;font-size:12.5px'><tr><th>店铺</th><th>素材ID</th><th>素材名称</th><th>消耗</th><th>净成交金额</th><th>总成交金额</th><th>ROI</th><th>环比</th><th>同比</th></tr>";
            okShops.forEach(s=>{
                const hl = (s.advertiser_id === String(aid));
                h += "<tr" + (hl ? " style='background:#eef4ff;font-weight:700'" : "") + ">";
                h += "<td>" + esc(s.name) + (hl ? "（当前）" : "") + "</td>";
                h += "<td style='font-family:monospace;font-size:11px'>" + esc(s.material_id || "") + "</td>";
                h += "<td style='max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' title='" + esc(s.素材名称 || "") + "'>" + esc(s.素材名称 || "—") + "</td>";
                h += "<td>" + fmtMoney(s.消耗) + "</td>";
                h += "<td>" + fmtMoney(s.净成交金额) + "</td>";
                h += "<td>" + fmtMoney(s.成交金额) + "</td>";
                h += "<td>" + (s.支付ROI||0).toFixed(2) + "</td>";
                h += "<td>" + trendTag(s.trend) + "</td>";
                h += "<td>" + trendTag(s.yoy_trend) + "</td></tr>";
            });
            h += "</table></div>";
        }else{
            h += "<div class='muted' style='margin-top:8px'>当前仅 1 个店铺有该素材数据" + (shops.some(s=>!s.ok) ? "（其余店铺未授权或查询失败）" : "") + "</div>";
        }
        h += "</div>";
        box.innerHTML = h;
    }catch(e){
        box.innerHTML = "<div class='muted' style='margin-top:12px'>跨店铺对比加载失败：" + esc(String(e)) + "</div>";
    }
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

// ===== 商品下拉框：店铺→商品→素材联动 =====
let productOptions = [];
async function loadProducts(){
    const sel = document.getElementById("productSelect");
    const aid = document.getElementById("matAdvertiser").value;
    sel.innerHTML = "<option value=''>加载商品中…</option>";
    try{
        const res = await (await fetch("/api/products" + (aid ? ("?advertiser_id="+encodeURIComponent(aid)) : ""))).json();
        productOptions = (res.success && res.data) ? res.data : [];
        sel.innerHTML = "";
        // 只显示有素材的商品（千川商品→素材仅存在于计划挂载关系，未投放过素材的商品无法查到素材）
        const hasMat = productOptions.filter(p=>p.has_materials);
        const all = document.createElement("option");
        all.value = ""; all.textContent = "全部商品（素材为全部）";
        sel.appendChild(all);
        hasMat.forEach(p=>{
            const o = document.createElement("option");
            o.value = p.id;
            o.textContent = p.name + `（${p.material_count}素材）`;
            sel.appendChild(o);
        });
        if(hasMat.length < productOptions.length){
            const tip = document.createElement("option");
            tip.value = ""; tip.disabled = true;
            tip.textContent = `共${hasMat.length}个商品有投放素材（另有${productOptions.length - hasMat.length}个商品暂无投放素材）`;
            sel.appendChild(tip);
        }
    }catch(e){
        sel.innerHTML = "<option value=''>商品加载失败</option>";
    }
}
function onProductChange(){
    document.getElementById("matDetail").innerHTML = "";
    const pid = document.getElementById("productSelect").value;
    if(pid){
        loadMaterialsByProduct(pid);
    }else{
        document.getElementById("productAgg").innerHTML = "";
        loadMaterialList();
    }
}
async function loadMaterialsByProduct(pid){
    const sel = document.getElementById("materialSelect");
    const aggBox = document.getElementById("productAgg");
    sel.innerHTML = "<option>加载该商品素材…</option>";
    aggBox.innerHTML = "<p class='spin' style='margin:8px 0 0'>正在汇总该商品所有素材的投放数据…</p>";
    const aid = document.getElementById("matAdvertiser").value;
    try{
        const res = await (await fetch("/api/materials_by_product?advertiser_id=" + encodeURIComponent(aid) + "&product_id=" + encodeURIComponent(pid))).json();
        if(!res.success){ sel.innerHTML = "<option>加载失败：" + (res.error||"") + "</option>"; aggBox.innerHTML = ""; return; }
        const items = res.data || [];
        if(!items.length){
            aggBox.innerHTML = "<div class='muted' style='margin:8px 0 0'>该商品暂无关联的投放素材（千川中商品与素材通过投放计划关联，该商品近90天未投放过素材）。</div>";
        }
        materialOptions = items;
        renderMaterialOptions();
        const agg = res.agg;
        if(agg){
            let h = "<h4 style='font-size:14px;margin:0 0 8px'>该商品所有素材投放数据</h4>";
            h += "<div class='kpi' style='margin-bottom:4px'>";
            h += kpi("素材数", fmtNum(agg.素材数));
            h += kpi("总消耗", fmtMoney(agg.消耗));
            h += kpi("净成交金额", agg.净成交金额 ? fmtMoney(agg.净成交金额) : "—");
            h += kpi("总成交金额", fmtMoney(agg.成交金额));
            h += kpi("成交单数", fmtNum(agg.成交单数));
            h += kpi("ROI", (agg.支付ROI||0).toFixed(2));
            h += "</div>";
            h += "<div class='muted' style='font-size:12px'>下拉框仅展示该商品下 " + items.length + " 个素材；数据来源为该商品在投计划挂载的素材。</div>";
            aggBox.innerHTML = h;
        }else{
            aggBox.innerHTML = "";
        }
    }catch(e){
        sel.innerHTML = "<option>加载失败</option>";
        aggBox.innerHTML = "";
    }
}

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
        // 同一素材 · 各店铺对比：放在“该商品所有素材投放数据”（商品聚合卡）正下方
        let html = "<div id='multiShopBox'><p class='spin' style='margin:0 0 12px'>正在加载同一素材 · 各店铺对比…</p></div>";
        html += "<div class='preview-box'>";
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
        // 素材画像：素材来源 / 创意方式 / 素材样式（尺寸）+ AI 样式识别（场景图/模特图/白底图）
        {
            const mm = s.material_meta || {};
            const srcCn = {E_COMMERCE:"本地上传",CREATIVE_CENTER:"巨量创意PC",STAR:"星图·即合",LIVE_HIGHLIGHT:"直播剪辑",JI_CHUANG:"即创",ARTHUR:"亚瑟",VIDEO_CAPTURE:"易拍APP",AGENT:"巨量方舟",AWEME:"抖音主页",SQUARE:"商品图",TADA:"tada"}[mm.source] || mm.source || "—";
            const modeCn = {VIDEO_VERTICAL:"竖版视频",VIDEO_LARGE:"横版视频",LARGE:"横版大图",LARGE_VERTICAL:"竖版大图",SMALL:"横版小图",SQUARE:"方图",UNION_SPLASH:"开屏图"}[mm.image_mode] || mm.image_mode || "—";
            const wayCn = {CUSTOM_CREATIVE:"自定义创意",PROGRAMMATIC_CREATIVE:"程序化创意"}[mm.creative_way] || mm.creative_way || "—";
            const aiBadge = mm.is_ai_create === true ? "<span style='color:#1f6feb'>AI生成</span>" : (mm.is_ai_create === false ? "人工素材" : "—");
            html += `<div style="display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px;font-size:12.5px;color:#1A1B1C;background:linear-gradient(135deg,rgba(139,200,234,0.08),rgba(139,200,234,0.02));border:1px solid #e7edf6;border-radius:10px;padding:8px 12px;">
                <span>📦 素材来源：<b>${esc(srcCn)}</b></span>
                <span>🎨 素材样式：<b>${esc(modeCn)}</b></span>
                <span>🧩 创意方式：<b>${esc(wayCn)}</b></span>
                <span>🤖 生成方式：${aiBadge}</span>
                <span id="aiStyleTag" style="color:#8a97ab">🔍 AI 样式识别中…</span>
            </div>`;
            // 异步调 AI 识图打标（场景图/模特图/白底图）
            (async function(){
                try{
                    const r = await fetch("/api/material_style?material_id=" + encodeURIComponent(mid) + "&mtype=" + encodeURIComponent(s.type) + (aid ? ("&advertiser_id="+encodeURIComponent(aid)) : ""));
                    const j = await r.json();
                    const tag = document.getElementById("aiStyleTag");
                    if(tag && j.success && j.style && j.style !== "未知"){
                        const colors = {"场景图":"#16a34a","模特图":"#7c3aed","白底图":"#1f6feb","其他":"#d97706"};
                        const c = colors[j.style] || "#6B7280";
                        tag.innerHTML = `AI 样式识别：<b style="color:${c}">${esc(j.style)}</b>` + (j.reason ? `<span class='muted' style='margin-left:6px'>（${esc(j.reason)}）</span>` : "");
                    }else if(tag){
                        tag.innerHTML = `AI 样式识别：<span class='muted'>${esc(j.note || j.style || "无法识别")}</span>`;
                    }
                }catch(e){
                    const tag = document.getElementById("aiStyleTag");
                    if(tag){ tag.innerHTML = "AI 样式识别：<span class='muted'>识别失败</span>"; }
                }
            })();
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
            html += "<details open style='margin-top:10px'><summary class='muted'>逐日明细（点击展开）</summary>";
            html += "<div style='max-height:260px;overflow-y:auto;border:1px solid #e7edf6;border-radius:8px;'><table class='data' style='margin:0;width:100%'><tr><th>日期</th><th>消耗</th><th>展示</th><th>点击</th><th>成交单</th><th>成交金额</th></tr>";
            daily.forEach(d=>{
                html += `<tr><td>${d.date}</td><td>${d.cost}</td><td>${d.show}</td><td>${d.click}</td><td>${d.orders}</td><td>${d.gmv}</td></tr>`;
            });
            html += "</table></div></details>";
        }
        html += "<div class='ai-head' style='justify-content:space-between'>AI 点评与修改建议";
        html += "<span style='display:flex;align-items:center;gap:8px;font-weight:400'>";
        html += "<button class='ghost' style='font-weight:400' onclick='openAiCtxModal(\\"links\\")'>竞品链接</button>";
        html += "<button class='ghost' style='font-weight:400' onclick='openAiCtxModal(\\"market\\")'>行业数据</button>";
        html += "<span id='aiCtxBadge' style='font-size:12px;color:#8a97ab'></span></span></div>";
        html += "<div class='ai-box'>" + renderAiViz(res.ai, s) + "</div>";
        box.innerHTML = html;
        loadAiCtx();
        loadMultiShop(mid, aid);
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
    // 1) AI 结论 → 纯可视化：阶段徽章 + 六维健康度面板（全部由真实数据计算，无文字分析）
    const stage = judgeStage(s);
    const ctr = parseFloat(s.点击率) || 0;
    const cvr = parseFloat(s.转化率) || 0;
    const roi = parseFloat(s.支付ROI) || 0;
    const cost = parseFloat(s.消耗) || 0;
    const days = Number(s.active_days) || 0;
    const trend = s.trend || "平稳";
    const trendMeta = trend === "上升" ? {c:"#16a34a", p:100}
        : (trend === "下滑" ? {c:"#dc2626", p:30} : {c:"#1f6feb", p:60});
    const gCtr  = gradeOf(ctr, 3, 1.5, 0.8);
    const gCvr  = gradeOf(cvr, 2, 1, 0.5);
    const gRoi  = gradeOf(roi, 1.5, 1.2, 0.8);
    const gRun  = gradeOf(cost, 2000, 800, 200);      // 跑量能力：按累计消耗分档
    const gDays = gradeOf(days, 30, 15, 7);           // 投放稳定：按投放天数分档
    let html = `<div class="viz-head">
        <span class="viz-stage ${stage.cls}">${stage.name}</span>
        <span class="viz-note" style="margin:0">累计投放 <b>${days}</b> 天 · 近7天消耗 <b>${fmtMoney(s.recent7_cost||0)}</b>${s.trend ? " · 趋势 <b>"+esc(s.trend)+"</b>" : ""}</span>
    </div>`;
    html += `<div class="viz-grid">`;
    html += scoreBar("跑量能力", fmtMoney(cost), cost/3000*100, gRun.g, gRun.c, "");
    html += scoreBar("点击率CTR", ctr.toFixed(2), ctr/4*100, gCtr.g, gCtr.c, "%");
    html += scoreBar("转化率CVR", cvr.toFixed(2), cvr/3*100, gCvr.g, gCvr.c, "%");
    html += scoreBar("投资回报ROI", roi.toFixed(2), roi/2*100, gRoi.g, gRoi.c, "");
    html += scoreBar("消耗趋势", trend, trendMeta.p, trend, trendMeta.c, "");
    html += scoreBar("投放稳定", days + "天", days/30*100, gDays.g, gDays.c, "");
    html += `</div>`;
    // 2) AI 修改建议 → 建议卡片网格（行动项，保留精炼文字）
    html += renderAiVisual(text);
    return html;
}
// 把 AI 文本解析为可视化元素：建议章节→卡片网格；解析不到建议章节时保底全部显示，避免空白
function renderAiVisual(text){
    const raw = String(text==null?"":text).trim();
    if(!raw) return "<div class='muted'>（无返回）</div>";
    if(/失败|error/i.test(raw) && raw.length < 160){
        return `<div class="muted" style="color:#c0392b">${esc(raw)}</div>`;
    }
    const lines = raw.split(/\\r?\\n/).map(l=>l.trim()).filter(l=>l.length>0);
    if(!lines.length) return "<div class='muted'>（无返回）</div>";
    const secs = [];
    let cur = null;
    lines.forEach(line=>{
        // 兼容多种章节写法：**一、xxx** / ## xxx / 1. xxx / 一、xxx
        let rl = line.replace(/^\\*{1,2}/, "").replace(/\\*{1,2}$/, "").trim();
        // 章节仅认中文序号或 markdown 标题；数字编号条目（1. xxx）归入上一章节的卡片
        const m = rl.match(/^(第?[一二三四五六七八九十百]+)[、.．:：)）]\\s*(.+)$/)
               || rl.match(/^#{1,6}\\s*(.+)$/);
        if(m){
            let title = (m[2] || m[1] || "").trim();
            const cm = title.match(/^([^：:]{1,10})[：:]\\s*(.+)$/);
            if(cm){ title = cm[1].trim(); }
            else{ title = title.replace(/[：:]\\s*$/, ""); }
            cur = {title: title, items: []};
            secs.push(cur);
            if(cm && cm[2].trim()){ cur.items.push(cm[2].trim()); }
        }else{
            if(!cur){ cur = {title: "AI 结论", items: []}; secs.push(cur); }
            cur.items.push(line);
        }
    });
    if(!secs.length){ secs.push({title: "AI 结论", items: lines}); }
    const ADV = /建议|优化|修改|改法|方向|行动|提升|加强|关注/;
    // 保底：解析不到任何建议章节时，全部章节都渲染为卡片，保证"点评与修改建议"区域有内容
    const hasAdvice = secs.some(sec=>ADV.test(sec.title));
    let html = "";
    secs.forEach(sec=>{
        if(hasAdvice && !ADV.test(sec.title)){ return; }
        html += `<div class="viz-sec"><span class="viz-sec-tag">${esc(sec.title)}</span></div>`;
        const cards = [];
        sec.items.forEach(line=>{
            const num = line.match(/^(\\d+)[、.．:：)）]\\s*(.+)$/);
            const circle = line.match(/^([①②③④⑤⑥⑦⑧⑨⑩])\\s*(.+)$/);
            const bold = line.match(/^\\*\\*(.+?)\\*\\*$/);
            if(num){ cards.push({t: num[2].trim()}); }
            else if(circle){ cards.push({t: circle[2].trim()}); }
            else if(bold){ cards.push({t: bold[1].trim()}); }
            else if(line.length){ cards.push({t: line}); }
        });
        html += `<div class="viz-cards">`;
        cards.forEach((c,i)=>{
            html += `<div class="viz-card"><span class="viz-no">${i+1}</span><span class="viz-txt">${aiInline(c.t)}</span></div>`;
        });
        html += `</div>`;
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
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@router.post("/api/upload")
async def upload_media(file: UploadFile = File(...)):
    filename = file.filename
    ext = filename.split(".")[-1].lower()
    image_ext = {"jpg", "jpeg", "png", "gif"}
    video_ext = {"mp4", "mov"}
    if ext in image_ext:
        f_type = "image"
    elif ext in video_ext:
        f_type = "video"
    else:
        return {"success": False, "msg": "仅支持图片、视频"}

    local_path = save_upload_file(file.file, filename)
    return UploadResp(success=True, local_file_path=local_path, file_name=filename, file_type=f_type)


@router.post("/api/upload_material")
async def upload_material(file: UploadFile = File(...)):
    """上传素材到上传素材库目录（upload_materials），成功后出现在上传素材库网格中。"""
    filename = (file.filename or "").replace("\\", "/").split("/")[-1]
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _IMAGE_EXT and ext not in _VIDEO_EXT:
        return {"success": False, "msg": "仅支持图片、视频文件"}
    if not os.path.isdir(UPLOAD_MATERIAL_DIR):
        try:
            os.makedirs(UPLOAD_MATERIAL_DIR, exist_ok=True)
        except OSError as e:
            return {"success": False, "msg": f"上传目录不可写：{e}"}
    base, e = os.path.splitext(filename)
    target = os.path.join(UPLOAD_MATERIAL_DIR, filename)
    i = 1
    while os.path.exists(target):  # 同名自动加序号，避免覆盖
        target = os.path.join(UPLOAD_MATERIAL_DIR, f"{base}({i}){e}")
        i += 1
    try:
        with open(target, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except OSError as e:
        return {"success": False, "msg": f"保存失败：{e}"}
    st = os.stat(target)
    return {"success": True, "data": {
        "name": os.path.basename(target), "path": target,
        "type": "image" if ext in _IMAGE_EXT else "video",
        "size": st.st_size, "mtime": st.st_mtime,
    }}


@router.get("/api/uploaded_materials")
async def uploaded_materials():
    """遍历上传素材库目录，返回全部上传的图片/视频文件列表（按上传时间倒序）。"""
    if not os.path.isdir(UPLOAD_MATERIAL_DIR):
        return {"success": True, "data": [], "dir": UPLOAD_MATERIAL_DIR, "count": 0}
    items = []
    for fn in os.listdir(UPLOAD_MATERIAL_DIR):
        ext = os.path.splitext(fn)[1].lower()
        if ext in _IMAGE_EXT:
            ftype = "image"
        elif ext in _VIDEO_EXT:
            ftype = "video"
        else:
            continue
        full = os.path.join(UPLOAD_MATERIAL_DIR, fn)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        items.append({"name": fn, "path": full, "type": ftype,
                      "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"success": True, "data": items, "dir": UPLOAD_MATERIAL_DIR, "count": len(items)}


@router.get("/api/uploaded_media/{name}")
async def uploaded_media(name: str):
    """返回上传素材库下的媒体文件流，做路径穿越防护。"""
    base = os.path.abspath(UPLOAD_MATERIAL_DIR)
    full = os.path.abspath(os.path.join(base, name))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(full)


@router.get("/api/local_materials")
async def local_materials():
    """遍历 C:\\test素材 目录，返回所有图片/视频文件列表（按文件名排序）。"""
    if not os.path.isdir(LOCAL_MATERIAL_DIR):
        return {"success": True, "data": [], "dir": LOCAL_MATERIAL_DIR, "count": 0,
                "error": f"目录不存在：{LOCAL_MATERIAL_DIR}"}
    items = []
    for fn in sorted(os.listdir(LOCAL_MATERIAL_DIR)):
        ext = os.path.splitext(fn)[1].lower()
        if ext in _IMAGE_EXT:
            ftype = "image"
        elif ext in _VIDEO_EXT:
            ftype = "video"
        else:
            continue
        full = os.path.join(LOCAL_MATERIAL_DIR, fn)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        items.append({"name": fn, "path": full, "type": ftype,
                      "size": st.st_size, "mtime": st.st_mtime})
    return {"success": True, "data": items, "dir": LOCAL_MATERIAL_DIR, "count": len(items)}


@router.get("/api/local_media/{name}")
async def local_media(name: str):
    """返回 C:\\test素材 下的媒体文件流（供图片显示/视频播放），做路径穿越防护。"""
    base = os.path.abspath(LOCAL_MATERIAL_DIR)
    full = os.path.abspath(os.path.join(base, name))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(full)
