
let ALL=[], STATUS={};
let curPage = 1;
const pageSize = 12;
let filterType = '全部';
let filterStatus = '全部';
let searchKeyword = '';
let viewMode = 'grid';
// 加载状态统计
fetch('/api/material_stats').then(r=>r.json()).then(j=>{
  if(j.error) return;
  document.querySelectorAll('.stat').forEach((el,i)=>{
    var lb = el.querySelector('.lb').textContent;
    if(j[lb] !== undefined) el.querySelector('.nv').textContent = j[lb];
  });
}).catch(()=>{});
function mapBiz(biz){
  if(!biz) return '未提交';
  if(biz==='待审核') return '审核中';
  if(biz==='通过-待投放') return '待投放';
  if(['直播间已投放','商城已投放','已投放商品+直播间'].includes(biz)) return '已投放';
  if(biz==='审核驳回') return '已暂停';
  if(biz==='放弃测试') return '已结束';
  return '未提交';
}
function stCls(s){
  return {'已投放':'st-live','审核中':'st-review','待投放':'st-wait','已暂停':'st-pause','已结束':'st-end','未提交':'st-pause'}[s]||'st-pause';
}
function infoOf(m){ return STATUS[m.path]||STATUS[m.name]||{}; }

// 加载分类和标签数据
async function loadFilterData(){
  try{
    // 加载分类体系
    const catRes = await fetch('/api/categories').then(r=>r.json());
    if(catRes.data){
      // 主类目（level=1）
      const mainCats = catRes.data.filter(c=>c.level===1);
      const mainHtml = mainCats.map(c=>`<button class="fopt" onclick="filterByTag(this,'主类目','${c.name}')">${c.name}</button>`).join('');
      document.getElementById('mainCatList').insertAdjacentHTML('beforeend', mainHtml);
      
      // 一级分类（level=2）
      const l1Cats = catRes.data.filter(c=>c.level===2);
      const l1Html = l1Cats.map(c=>`<button class="fopt" onclick="filterByTag(this,'一级分类','${c.name}')">${c.name}</button>`).join('');
      document.getElementById('l1CatList').insertAdjacentHTML('beforeend', l1Html);
      
      // 二级分类（level=3）
      const l2Cats = catRes.data.filter(c=>c.level===3);
      const l2Html = l2Cats.map(c=>`<button class="fopt" onclick="filterByTag(this,'二级分类','${c.name}')">${c.name}</button>`).join('');
      document.getElementById('l2CatList').insertAdjacentHTML('beforeend', l2Html);
    }
    
    // 加载状态标签
    const statusRes = await fetch('/api/material_biz_statuses').then(r=>r.json());
    if(statusRes.data){
      const statusHtml = statusRes.data.map(s=>`<button class="fopt" onclick="filterByStatus(this,'${s.name}')">${s.name}</button>`).join('');
      document.getElementById('statusList').insertAdjacentHTML('beforeend', statusHtml);
    }
    
    // 加载公共标签
    const tagRes = await fetch('/api/tags?tag_type=public').then(r=>r.json());
    if(tagRes.data){
      const tagHtml = tagRes.data.map(t=>`<button class="chip" onclick="toggleChip(this)">${t.name}</button>`).join('');
      document.getElementById('publicTagList').innerHTML = tagHtml;
    }
    
    // 加载个人标签
    const pTagRes = await fetch('/api/tags?tag_type=personal').then(r=>r.json());
    if(pTagRes.data){
      const pTagHtml = pTagRes.data.map(t=>`<button class="fopt" onclick="filterByTag(this,'个人标签','${t.name}')">${t.name}</button>`).join('');
      document.getElementById('personalTagList').insertAdjacentHTML('beforeend', pTagHtml);
    }
  }catch(e){
    console.error('加载筛选数据失败:', e);
  }
}

// 页面加载时调用
loadFilterData();

async function load(){
  try{
    const [mRes,sRes]=await Promise.all([
      fetch('/api/db_materials').then(r=>r.json()),
      fetch('/api/material_launch_status').then(r=>r.json()),
    ]);
    ALL=(mRes&&mRes.data)||[];
    STATUS=(sRes&&sRes.data)||{};
    render();
  }catch(e){
    document.getElementById('grid').innerHTML='<div style="grid-column:1/-1;padding:60px;text-align:center;color:#FF6B6B">加载素材失败：'+e.message+'</div>';
  }
}
function render(){
  const g=document.getElementById('grid');
  // 更新统计数字
  document.querySelector('.lt .cnt').textContent = ALL.length;
  document.querySelector('.lt .total').textContent = '共 ' + ALL.length + ' 份素材';
  if(!ALL.length){
    g.innerHTML='<div style="grid-column:1/-1;padding:60px;text-align:center;color:var(--text-3)">暂无上传素材，点右上"本地上传"添加</div>';
    document.querySelector('.pager span').textContent='共 0 条 · 每页 ' + pageSize + ' 条';
    document.querySelector('.pages').innerHTML='';
    return;
  }
  // 筛选
  var filtered = ALL;
  if(filterType !== '全部'){
    filtered = filtered.filter(m => (m.type||'image') === (filterType==='图片'?'image':'video'));
  }
  // 搜索筛选
  if(searchKeyword){
    var kw = searchKeyword.toLowerCase();
    filtered = filtered.filter(m => 
      (m.name||'').toLowerCase().includes(kw) || 
      String(m.id||'').includes(kw)
    );
  }
  if(filterStatus !== '全部'){
    filtered = filtered.filter(m => {
      var info = infoOf(m);
      var st = mapBiz(info.biz_status);
      return st === filterStatus;
    });
  }
  // 分页
  var totalPages = Math.ceil(filtered.length / pageSize);
  if(curPage > totalPages) curPage = totalPages;
  if(curPage < 1) curPage = 1;
  var start = (curPage - 1) * pageSize;
  var pageData = filtered.slice(start, start + pageSize);
  document.querySelector('.pager span').textContent='共 '+filtered.length+' 条 · 每页 ' + pageSize + ' 条';
  // 页码
  var pagesHtml = '<button class="pg" onclick="goPage(' + (curPage-1) + ')" ' + (curPage<=1?'disabled':'') + '>‹</button>';
  for(var i=1; i<=totalPages; i++){
    pagesHtml += '<button class="pg' + (i===curPage?' on':'') + '" onclick="goPage(' + i + ')">' + i + '</button>';
  }
  pagesHtml += '<button class="pg" onclick="goPage(' + (curPage+1) + ')" ' + (curPage>=totalPages?'disabled':'') + '>›</button>';
  document.querySelector('.pages').innerHTML = pagesHtml;
  g.innerHTML=pageData.map(m=>{
    const info=infoOf(m);
    const st=mapBiz(info.biz_status);
    const url='/api/uploaded_media/'+encodeURIComponent(m.name);
    const cover=m.type==='video'
      ? '<video src="'+url+'" muted preload="auto" playsinline controlslist="nodownload"></video>'
      : '<img src="'+url+'" alt="">';
    const fn=m.name.replace(/\.[^.]+$/,'');
    const sz=m.size? (m.size>1048576?(m.size/1048576).toFixed(1)+' MB':Math.round(m.size/1024)+' KB'):'';
    const time=m.mtime?new Date(m.mtime*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
    const goto='15-素材详情-新版.html?name='+encodeURIComponent(m.name)+'&type='+encodeURIComponent(m.type||'');
    return '<div class="m" style="cursor:pointer" onclick="openDetail(\''+goto+'\')">'+
      '<div class="cover">'+cover+
        '<div class="chk" onclick="event.stopPropagation();this.classList.toggle(\'on\');this.closest(\'.m\').classList.toggle(\'sel\');updateFloatBar()"></div>'+
        '<span class="tag-l">'+(m.type==='video'?'视频':'图片')+'</span>'+
        '<span class="tag-r '+stCls(st)+'">'+st+'</span>'+
      '</div>'+
      '<div class="data">'+
        '<span>成交 <b>—</b></span><span>点击率 <b>—</b></span>'+
        '<span>转化率 <b>—</b></span><span>ROI <b>—</b></span>'+
      '</div>'+
      '<div class="info">'+
        '<div class="ttl">'+fn+'</div>'+
        '<div class="meta">'+(info.plan_name||'本地上传')+' · '+sz+'</div>'+
        '<div class="tags"><span class="hi">'+st+'</span></div>'+
      '</div>'+
      '<div class="foot"><span class="spend">消耗 ¥—</span><span>曝光 —</span></div>'+
      '<div class="foot" style="border-top:none;padding-top:2px"><span>本地上传</span><span>'+time+'</span></div>'+
    '</div>';
  }).join('');
}


function toggleChip(el){
  el.classList.toggle('on');
  curPage = 1;
  render();
}
function clearAllTags(){
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
  curPage = 1;
  render();
}
function filterBySelect(el, category){
  var value = '';
  if(el.tagName === 'SELECT'){
    value = el.options[el.selectedIndex].text;
  } else if(el.tagName === 'INPUT'){
    value = el.value;
  }
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}
function resetAllFilters(){
  window.filters = {};
  filterStatus = '全部';
  filterType = '全部';
  // 重置所有按钮状态
  document.querySelectorAll('.fopt').forEach((el,i)=>{
    el.classList.remove('active');
  });
  document.querySelectorAll('.frow:first-child .fopt:first-child').forEach(el=>el.classList.add('active'));
  curPage = 1;
  render();
}

function filterByTag(el, category, value){
  el.parentElement.querySelectorAll('.fopt').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  // 存储筛选条件
  if(!window.filters) window.filters = {};
  window.filters[category] = value;
  curPage = 1;
  render();
}
function filterByType(el, type){
  el.parentElement.querySelectorAll('.fopt').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  filterType = type;
  curPage = 1;
  render();
}
function toggleView(mode){
  viewMode = mode;
  document.getElementById('gridBtn').classList.toggle('primary', mode==='grid');
  document.getElementById('listBtn').classList.toggle('primary', mode==='list');
  render();
}
function onSearch(){
  searchKeyword = document.getElementById('searchInput').value.trim();
  curPage = 1;
  render();
}
function filterByStatCard(status, el){
  filterStatus = status;
  // 同步选中下面的计划阶段筛选
  document.querySelectorAll('.stat').forEach(s=>s.classList.remove('accent'));
  el.classList.add('accent');
  // 同步选中筛选区的计划阶段选项
  document.querySelectorAll('.frow .fopts .fopt').forEach(x=>{
    if(x.textContent.trim() === status){
      x.parentElement.querySelectorAll('.fopt').forEach(y=>y.classList.remove('active'));
      x.classList.add('active');
    }
  });
  curPage = 1;
  render();
}
function filterByStatus(el, status){
  el.parentElement.querySelectorAll('.fopt').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  filterStatus = status;
  curPage = 1;
  render();
}
function goPage(p){
  curPage = p;
  render();
}
function getSelCount(){return document.querySelectorAll('.m .chk.on').length;}
function updateFloatBar(){
  document.getElementById('selText').textContent=getSelCount();
}
function toggleAll(ck){
  document.querySelectorAll('.m .chk').forEach(function(x){
    if(ck && !x.classList.contains('on')){x.click();}
    if(!ck && x.classList.contains('on')){x.click();}
  });
}
function clearSel(){
  document.querySelectorAll('.chk.on').forEach(c=>c.classList.remove('on'));
  document.querySelectorAll('.m.sel').forEach(c=>c.classList.remove('sel'));
  updateFloatBar();
}
function openLaunchModal(){
  if(getSelCount()===0) return;
  var f=document.getElementById('launchFrame');
  f.src='14-上传到投放计划.html?count='+getSelCount();
  document.getElementById('launchModal').style.display='flex';
}
// 详情弹窗
function openDetail(url){
  var m=document.getElementById('detailModal');
  document.getElementById('detailFrame').src=url;
  m.style.display='flex';
}
function closeDetail(){document.getElementById('detailModal').style.display='none';}
function closeLaunchModal(){document.getElementById('launchModal').style.display='none';}
function nextStep(){
  const shop=document.getElementById('shopSel').value;
  if(!shop){alert('请先选择投放店铺');return;}
  alert('下一步：选择计划与商品（待实现）\n店铺：'+document.getElementById('shopSel').selectedOptions[0].textContent);
}
// 监听 checkbox 变化（事件委托）
document.addEventListener('click',e=>{
  if(e.target.classList&&e.target.classList.contains('chk')){
    setTimeout(updateFloatBar,0);
  }
});
load();
function openSetting(url){
  document.getElementById('settingFrame').src=url + (url.indexOf('?')>=0?'&':'?') + '_t=' + Date.now();
  document.getElementById('settingModal').style.display='flex';
}
function closeSetting(){
  document.getElementById('settingModal').style.display='none';
  document.getElementById('settingFrame').src='about:blank';
}
