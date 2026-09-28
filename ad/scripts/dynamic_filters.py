with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 替换主类目筛选区域，改为动态渲染
old_main_cat = '''<div class="frow"><div class="fl">主类目</div><div class="fopts">
      <button class="fopt active" onclick="filterByTag(this,'主类目','全部')">全部</button><button class="fopt" onclick="filterByTag(this,'主类目','服饰BU')">服饰BU</button><button class="fopt" onclick="filterByTag(this,'主类目','配饰BU')">配饰BU</button><button class="fopt" onclick="filterByTag(this,'主类目','家居BU')">家居BU</button>
    </div></div>'''

new_main_cat = '''<div class="frow"><div class="fl">主类目</div><div class="fopts" id="mainCatList">
      <button class="fopt active" onclick="filterByTag(this,'主类目','全部')">全部</button>
    </div></div>'''

content = content.replace(old_main_cat, new_main_cat)

# 2. 替换一级分类筛选区域，改为动态渲染
old_l1_cat = '''<div class="frow"><div class="fl">一级分类</div><div class="fopts">
      <button class="fopt active" onclick="filterByTag(this,'一级分类','全部')">全部</button><button class="fopt" onclick="filterByTag(this,'一级分类','衬衫')">衬衫</button><button class="fopt" onclick="filterByTag(this,'一级分类','针织')">针织</button><button class="fopt" onclick="filterByTag(this,'一级分类','外套')">外套</button><button class="fopt" onclick="filterByTag(this,'一级分类','包袋')">包袋</button><button class="fopt" onclick="filterByTag(this,'一级分类','鞋履')">鞋履</button><button class="fopt" onclick="filterByTag(this,'一级分类','家纺')">家纺</button><button class="fopt" onclick="filterByTag(this,'一级分类','生活用品')">生活用品</button>
    </div></div>'''

new_l1_cat = '''<div class="frow"><div class="fl">一级分类</div><div class="fopts" id="l1CatList">
      <button class="fopt active" onclick="filterByTag(this,'一级分类','全部')">全部</button>
    </div></div>'''

content = content.replace(old_l1_cat, new_l1_cat)

# 3. 替换二级分类筛选区域，改为动态渲染
old_l2_cat = '''<div class="frow"><div class="fl"></div><div class="fopts">
      <button class="fopt active" onclick="filterByTag(this,'二级分类','全部')">全部</button><button class="fopt" onclick="filterByTag(this,'二级分类','亚麻衬衫')">亚麻衬衫</button><button class="fopt" onclick="filterByTag(this,'二级分类','条纹衬衫')">条纹衬衫</button><button class="fopt" onclick="filterByTag(this,'二级分类','通勤衬衫')">通勤衬衫</button><button class="fopt" onclick="filterByTag(this,'二级分类','针织开衫')">针织开衫</button><button class="fopt" onclick="filterByTag(this,'二级分类','基础针织')">基础针织</button><button class="fopt" onclick="filterByTag(this,'二级分类','休闲外套')">休闲外套</button><button class="fopt" onclick="filterByTag(this,'二级分类','轻薄外套')">轻薄外套</button><button class="fopt" onclick="filterByTag(this,'二级分类','通勤包')">通勤包</button><button class="fopt" onclick="filterByTag(this,'二级分类','旅行包')">旅行包</button><button class="fopt" onclick="filterByTag(this,'二级分类','休闲鞋')">休闲鞋</button><button class="fopt" onclick="filterByTag(this,'二级分类','通勤鞋')">通勤鞋</button><button class="fopt" onclick="filterByTag(this,'二级分类','床品')">床品</button><button class="fopt" onclick="filterByTag(this,'二级分类','毛巾')">毛巾</button><button class="fopt" onclick="filterByTag(this,'二级分类','水杯')">水杯</button><button class="fopt" onclick="filterByTag(this,'二级分类','收纳')">收纳</button>
    </div></div>'''

new_l2_cat = '''<div class="frow"><div class="fl"></div><div class="fopts" id="l2CatList">
      <button class="fopt active" onclick="filterByTag(this,'二级分类','全部')">全部</button>
    </div></div>'''

content = content.replace(old_l2_cat, new_l2_cat)

# 4. 替换计划阶段筛选区域，改为动态渲染
old_status = '''<div class="frow"><div class="fl">计划阶段</div><div class="fopts">
      <button class="fopt active" onclick="filterByStatus(this,'全部')">全部</button><button class="fopt" onclick="filterByStatus(this,'审核中')">审核中</button><button class="fopt" onclick="filterByStatus(this,'待投放')">待投放</button><button class="fopt" onclick="filterByStatus(this,'已投放')">已投放</button><button class="fopt" onclick="filterByStatus(this,'已暂停')">已暂停</button><button class="fopt" onclick="filterByStatus(this,'已结束')">已结束</button><button class="fopt" onclick="filterByStatus(this,'未提交')">未提交</button>
    </div></div>'''

new_status = '''<div class="frow"><div class="fl">计划阶段</div><div class="fopts" id="statusList">
      <button class="fopt active" onclick="filterByStatus(this,'全部')">全部</button>
    </div></div>'''

content = content.replace(old_status, new_status)

# 5. 替换公共标签区域，改为动态渲染
old_public_tags = '''<div class="frow"><div class="fl">公共标签</div><div class="fopts">
      <input class="fsearch" placeholder="Q 搜索标签">
      <button class="fopt" onclick="alert('打开标签分组')">浏览标签分组</button><button class="fopt active" onclick="filterByTag(this,'公共标签','全部')">全部</button>
      <button class="fopt" onclick="filterByTag(this,'公共标签','卖点')">卖点</button><button class="fopt" onclick="filterByTag(this,'公共标签','内容类型')">内容类型</button><button class="fopt" onclick="filterByTag(this,'公共标签','场景')">场景</button><button class="fopt" onclick="filterByTag(this,'公共标签','营销节点')">营销节点</button><button class="fopt" onclick="clearAllTags()">清除标签条件</button>
    </div></div>
    <div class="frow"><div class="fl"></div><div class="fopts" style="flex-direction:column;gap:8px">
      <div style="font-size:12px;color:var(--text-4)">已选标签匹配方式 <button class="fopt" style="display:inline-block">全部满足 ▾</button>　需同时包含所有已选公共标签 · 已选 0 个</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="chip on" onclick="toggleChip(this)">舒适透气</button><button class="chip on" onclick="toggleChip(this)">显瘦版型</button><button class="chip on" onclick="toggleChip(this)">质感面料</button><button class="chip on" onclick="toggleChip(this)">百城通勤</button>
        <button class="chip" onclick="toggleChip(this)">商品展示</button><button class="chip" onclick="toggleChip(this)">穿搭推荐</button><button class="chip" onclick="toggleChip(this)">细节特写</button>
        <button class="chip warn" onclick="toggleChip(this)">日常通勤</button><button class="chip warn" onclick="toggleChip(this)">周末出游</button><button class="chip warn" onclick="toggleChip(this)">国家生活</button>
        <button class="chip warn" onclick="toggleChip(this)">秋季上新</button><button class="chip warn" onclick="toggleChip(this)">双11预热</button>
      </div>
    </div></div>'''

new_public_tags = '''<div class="frow"><div class="fl">公共标签</div><div class="fopts">
      <input class="fsearch" placeholder="Q 搜索标签">
      <button class="fopt" onclick="alert('打开标签分组')">浏览标签分组</button><button class="fopt active" onclick="filterByTag(this,'公共标签','全部')">全部</button>
      <button class="fopt" onclick="clearAllTags()">清除标签条件</button>
    </div></div>
    <div class="frow"><div class="fl"></div><div class="fopts" style="flex-direction:column;gap:8px">
      <div style="font-size:12px;color:var(--text-4)">已选标签匹配方式 <button class="fopt" style="display:inline-block">全部满足 ▾</button>　需同时包含所有已选公共标签 · 已选 <span id="selectedTagCount">0</span> 个</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="publicTagList">
      </div>
    </div></div>'''

content = content.replace(old_public_tags, new_public_tags)

# 6. 替换个人标签区域，改为动态渲染
old_personal_tags = '''<div class="frow"><div class="fl">个人标签</div><div class="fopts">
      <input class="fsearch" placeholder="Q 搜索标签">
      <button class="fopt active" onclick="filterByTag(this,'个人标签','全部')">全部</button><button class="fopt" onclick="filterByTag(this,'个人标签','无个人标签')">无个人标签</button><button class="fopt" onclick="filterByTag(this,'个人标签','有个人标签')">有个人标签</button>
      <button class="fopt" onclick="filterByTag(this,'个人标签','重点关注')">重点关注</button><button class="fopt" onclick="filterByTag(this,'个人标签','待复盘')">待复盘</button><button class="fopt" onclick="resetAllFilters()">重置</button><button class="fopt" style="color:var(--green)" onclick="alert('打开标签管理')">✎ 管理标签</button>
    </div></div>'''

new_personal_tags = '''<div class="frow"><div class="fl">个人标签</div><div class="fopts" id="personalTagList">
      <input class="fsearch" placeholder="Q 搜索标签">
      <button class="fopt active" onclick="filterByTag(this,'个人标签','全部')">全部</button>
      <button class="fopt" onclick="resetAllFilters()">重置</button><button class="fopt" style="color:var(--green)" onclick="alert('打开标签管理')">✎ 管理标签</button>
    </div></div>'''

content = content.replace(old_personal_tags, new_personal_tags)

# 7. 添加加载分类和标签的JS函数
load_js = '''
// 加载分类和标签数据
async function loadFilterData(){
  try{
    // 加载分类体系
    const catRes = await fetch('/api/material_categories').then(r=>r.json());
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
    const tagRes = await fetch('/api/material_tags?tag_type=public').then(r=>r.json());
    if(tagRes.data){
      const tagHtml = tagRes.data.map(t=>`<button class="chip" onclick="toggleChip(this)">${t.name}</button>`).join('');
      document.getElementById('publicTagList').innerHTML = tagHtml;
    }
    
    // 加载个人标签
    const pTagRes = await fetch('/api/material_tags?tag_type=personal').then(r=>r.json());
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
'''

# 在 load 函数前添加
content = content.replace('async function load(){', load_js + '\nasync function load(){')

with open('C:/Users/33082/PycharmProjects/wellflow-saas-backend/static/wellflow-html/04-投放台.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 动态加载筛选标签完成')
