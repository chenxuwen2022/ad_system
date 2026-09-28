# 修改投放台页面
file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 修改 openDetailByName 函数，传索引
old = """function openDetailByName(name){
  const m = ALL.find(x => x.name === name);
  if(!m) return;
  const imgUrl = '/api/uploaded_media/' + encodeURIComponent(m.name);
  // 把素材信息存到 sessionStorage
  sessionStorage.setItem('currentMaterial', JSON.stringify({
    name: m.name,
    type: m.type,
    path: m.path,
    imgUrl: imgUrl,
    biz_status: m.biz_status,
    mtime: m.mtime,
    size: m.size
  }));
  openDetail('15-素材详情-新版.html');
}"""

new = """function openDetailByName(name){
  const idx = ALL.findIndex(x => x.name === name);
  if(idx < 0) return;
  openDetail('15-素材详情-新版.html?idx=' + idx);
}"""

if old in content:
    content = content.replace(old, new)
    print("✅ 修改投放台页面成功")
else:
    print("❌ 没找到投放台页面的旧内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

# 修改素材详情页面，从接口获取所有素材，然后根据索引找到对应的素材
file_path2 = r"static\wellflow-html\15-素材详情-新版.html"

with open(file_path2, "r", encoding="utf-8") as f:
    content2 = f.read()

old2 = """// 从 sessionStorage 读取素材信息
const matStr = sessionStorage.getItem('currentMaterial') || '{}';
const mat = JSON.parse(matStr);
const MNAME = mat.name || "";
const MTYPE = mat.type || "";
const MPATH = mat.path || "";
const IMGURL = mat.imgUrl || "";
const MAT = mat;

// 填充基本信息
if (MNAME) {
  var fn = MNAME;
  var ext = fn.split(".").pop().toLowerCase();
  var isVideo = ["mp4","mov","avi","mkv","webm"].indexOf(ext) >= 0;
  var idName = fn.replace(/\\.[^.]+$/, "");
  document.getElementById("matName").textContent = idName;
  document.getElementById("matId").textContent = "ID " + idName + " · 成片 · " + (isVideo ? "视频" : "图片");
  document.title = idName + " · WellFlow";
  // 直接用传过来的图片 URL
  const url = IMGURL;
  const player = document.getElementById("playerBox");
  if (isVideo) {
    player.innerHTML = `<video src="${url}" muted controls autoplay loop></video><span class="badge">本地素材</span>`;
  } else {
    player.innerHTML = `<img src="${url}" alt=""><span class="badge">本地素材</span>`;
  }
}"""

new2 = """// 从 URL 获取索引，然后从接口获取素材列表
const qs = new URLSearchParams(location.search);
const MAT_IDX = parseInt(qs.get("idx") || "0");
let MAT = {};
let MNAME = "";
let MTYPE = "";
let MPATH = "";
let IMGURL = "";

async function initMaterial() {
  try {
    const r = await fetch('/api/db_materials');
    const j = await r.json();
    if (j.success && j.data && j.data[MAT_IDX]) {
      MAT = j.data[MAT_IDX];
      MNAME = MAT.name;
      MTYPE = MAT.type;
      MPATH = MAT.path;
      IMGURL = '/api/uploaded_media/' + encodeURIComponent(MNAME);
      
      // 填充基本信息
      var ext = MNAME.split(".").pop().toLowerCase();
      var isVideo = ["mp4","mov","avi","mkv","webm"].indexOf(ext) >= 0;
      var idName = MNAME.replace(/\\.[^.]+$/, "");
      document.getElementById("matName").textContent = idName;
      document.getElementById("matId").textContent = "ID " + idName + " · 成片 · " + (isVideo ? "视频" : "图片");
      document.title = idName + " · WellFlow";
      const player = document.getElementById("playerBox");
      if (isVideo) {
        player.innerHTML = `<video src="${IMGURL}" muted controls autoplay loop></video><span class="badge">本地素材</span>`;
      } else {
        player.innerHTML = `<img src="${IMGURL}" alt=""><span class="badge">本地素材</span>`;
      }
    }
  } catch(e) { console.error(e); }
}
initMaterial();"""

if old2 in content2:
    content2 = content2.replace(old2, new2)
    print("✅ 修改素材详情页面成功")
else:
    print("❌ 没找到素材详情页面的旧内容")

with open(file_path2, "w", encoding="utf-8") as f:
    f.write(content2)

print("完成")
