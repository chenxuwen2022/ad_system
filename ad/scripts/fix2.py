file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 修改 render 函数，用索引传递
old_render = """  g.innerHTML=pageData.map(m=>{
    const info=infoOf(m);
    const st=mapBiz(info.biz_status);
    const url='/api/uploaded_media/'+encodeURIComponent(m.name);
    const cover=m.type==='video'
      ? '<video src="'+url+'" muted preload="auto" playsinline controlslist="nodownload"></video>'
      : '<img src="'+url+'" alt="">';
    const fn=m.name.replace(/\.[^.]+$/,'');
    const sz=m.size? (m.size>1048576?(m.size/1048576).toFixed(1)+' MB':Math.round(m.size/1024)+' KB'):'';
    const time=m.mtime?new Date(m.mtime*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
    const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '');
    return '<div class="m" style="cursor:pointer" onclick="openDetail(this.dataset.url)" data-url="' + detailUrl + '">"+
      '<div class="cover">'+cover+
        '<div class="chk" onclick="event.stopPropagation();this.classList.toggle(\'on\');this.closest(\'.m\').classList.toggle(\'sel\');updateFloatBar()"></div>'+
        '<span class="tag-l">'+(m.type==='video'?'视频':'图片')+'</span>'+
        '<span class="tag-r '+stCls(st)+'">'+st+'</span>'+
      '</div>'+
      '<div class="data">'+
        '<span>成交 <b>—</b></span><span>点击率 <b>—</b></span>'+
        '<span>转化率 <b>—</b></span><span>ROI <b>—</b></span>'+
      '</div>'+
      '<div class="nm">'+fn+'</div>'+
      '<div class="sub">'+time+' · '+sz+'</div>'+
    '</div>';
  }).join('');"""

new_render = """  g.innerHTML=pageData.map((m, idx)=>{
    const info=infoOf(m);
    const st=mapBiz(info.biz_status);
    const url='/api/uploaded_media/'+encodeURIComponent(m.name);
    const cover=m.type==='video'
      ? '<video src="'+url+'" muted preload="auto" playsinline controlslist="nodownload"></video>'
      : '<img src="'+url+'" alt="">';
    const fn=m.name.replace(/\.[^.]+$/,'');
    const sz=m.size? (m.size>1048576?(m.size/1048576).toFixed(1)+' MB':Math.round(m.size/1024)+' KB'):'';
    const time=m.mtime?new Date(m.mtime*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
    return '<div class="m" style="cursor:pointer" onclick="openDetailByIndex('+(startIdx+idx)+')">'+
      '<div class="cover">'+cover+
        '<div class="chk" onclick="event.stopPropagation();this.classList.toggle(\'on\');this.closest(\'.m\').classList.toggle(\'sel\');updateFloatBar()"></div>'+
        '<span class="tag-l">'+(m.type==='video'?'视频':'图片')+'</span>'+
        '<span class="tag-r '+stCls(st)+'">'+st+'</span>'+
      '</div>'+
      '<div class="data">'+
        '<span>成交 <b>—</b></span><span>点击率 <b>—</b></span>'+
        '<span>转化率 <b>—</b></span><span>ROI <b>—</b></span>'+
      '</div>'+
      '<div class="nm">'+fn+'</div>'+
      '<div class="sub">'+time+' · '+sz+'</div>'+
    '</div>';
  }).join('');"""

# 先找到 start 变量
if "const start = (curPage - 1) * pageSize;" in content:
    # 修改成 startIdx
    content = content.replace(
        "const start = (curPage - 1) * pageSize;",
        "const startIdx = (curPage - 1) * pageSize;"
    )
    content = content.replace("const pageData = filtered.slice(start, start + pageSize);", 
                              "const pageData = filtered.slice(startIdx, startIdx + pageSize);")

# 修改 render 函数
if old_render in content:
    content = content.replace(old_render, new_render)
    print("✅ 修改 render 函数成功")
else:
    print("❌ 没找到 render 函数的旧内容")

# 2. 修改 openDetail 函数，加上 openDetailByIndex
old_open = """function openDetail(url){
  var m=document.getElementById('detailModal');
  document.getElementById('detailFrame').src=url;
  m.style.display='flex';
}"""

new_open = """function openDetail(url){
  var m=document.getElementById('detailModal');
  document.getElementById('detailFrame').src=url;
  m.style.display='flex';
}
function openDetailByIndex(idx){
  const m = ALL[idx];
  if(!m) return;
  const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '');
  openDetail(detailUrl);
}"""

if old_open in content:
    content = content.replace(old_open, new_open)
    print("✅ 修改 openDetail 函数成功")
else:
    print("❌ 没找到 openDetail 函数的旧内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("完成")
