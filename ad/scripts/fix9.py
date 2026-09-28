# 修改投放台页面
file_path = r"static\wellflow-html\04-投放台.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 修改 openDetailByName 函数，用 sessionStorage 传参
old = """function openDetailByName(name){
  const m = ALL.find(x => x.name === name);
  if(!m) return;
  const imgUrl = '/api/uploaded_media/' + encodeURIComponent(m.name);
  const detailUrl = '15-素材详情-新版.html?name=' + encodeURIComponent(m.name) + '&type=' + encodeURIComponent(m.type || '') + '&path=' + encodeURIComponent(m.path || '') + '&imgUrl=' + encodeURIComponent(imgUrl);
  openDetail(detailUrl);
}"""

new = """function openDetailByName(name){
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

if old in content:
    content = content.replace(old, new)
    print("✅ 修改投放台页面成功")
else:
    print("❌ 没找到投放台页面的旧内容")

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

# 修改素材详情页面，从 sessionStorage 读取素材信息
file_path2 = r"static\wellflow-html\15-素材详情-新版.html"

with open(file_path2, "r", encoding="utf-8") as f:
    content2 = f.read()

old2 = """const qs = new URLSearchParams(location.search);
const MNAME = qs.get("name") || "";
const MTYPE = qs.get("type") || "";
const MPATH = qs.get("path") || "";
const IMGURL = qs.get("imgUrl") || "";

// 填充基本信息
if (MNAME) {
  var fn = MNAME;
  var ext = fn.split(".").pop().toLowerCase();
  var isVideo = ["mp4","mov","avi","mkv","webm"].indexOf(ext) >= 0;
  var idName = fn.replace(/\\.[^.]+$/, "");
  document.getElementById("matName").textContent = idName;
  document.getElementById("matId").textContent = "ID " + idName + " · 成片 · " + (isVideo ? "视频" : "图片");
  document.title = idName + " · WellFlow";
  // 优先用传过来的图片 URL
  const url = IMGURL || ("/api/uploaded_media/" + encodeURIComponent(fn));
  const player = document.getElementById("playerBox");
  if (isVideo) {
    player.innerHTML = `<video src="${url}" muted controls autoplay loop></video><span class="badge">本地素材</span>`;
  } else {
    player.innerHTML = `<img src="${url}" alt=""><span class="badge">本地素材</span>`;
  }
}"""

new2 = """// 从 sessionStorage 读取素材信息
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

if old2 in content2:
    content2 = content2.replace(old2, new2)
    print("✅ 修改素材详情页面成功")
else:
    print("❌ 没找到素材详情页面的旧内容")

with open(file_path2, "w", encoding="utf-8") as f:
    f.write(content2)

print("完成")
