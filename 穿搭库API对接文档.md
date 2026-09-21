# 穿搭库 API 对接文档(前端联调用)

> 版本:2026-09-16 | 对接人:前端同学 | 后端:wellflow-saas-backend-new

## 一、服务信息

| 项 | 值 |
|---|---|
| 本地服务 | `http://localhost:8000`(联调时后端启动 `python main.py`) |
| CORS | 已全开(`allow_origins=["*"]`),前端任意域名/端口可直接调用,无需代理 |
| 鉴权 | 无。`scope`(官方/我的)由前端自行传值,后端不校验身份 |
| 图片访问 | 接口返回的图片是**相对路径**(如 `/static/assets/outfits/wf-o004.png`),前端需拼接服务地址:完整 URL = `{base}/static/...` |
| 维度枚举 | 建议前端启动时调一次 `/api/outfit/dimensions` 动态渲染筛选面板,不要写死 |

## 二、全局响应约定

成功:
```json
{ "code": 0, "data": { ... } }
```
失败(业务错误):`{ "code": 1, "msg": "错误描述" }`
失败(参数/权限类,HTTP 非 200):FastAPI 默认格式 `{ "detail": "..." }`,如官方资产编辑返回 **HTTP 403**

## 三、穿搭记录数据结构(核心)

```json
{
  "id": "WF-O004",            // 素材编号,官方/我的统一 WF-Oxxx
  "category": "outfit",       // 固定
  "name": "藏青短裤凉拖搭配",
  "desc": "一句话素材描述",
  "tags": "极简,休闲,裤子,鞋", // 逗号分隔,搜索/卡片文案用
  "scope": "official",        // official=官方(只读) | mine=我的(可编辑删除)
  "origin": "ai",             // upload|url|demo|ai —— ai/demo 时详情页要展示 AI 拆解标注
  "cover": "/static/assets/outfits/wf-o004.png",      // 平铺总图(列表封面,4:3)
  "original": "/static/assets/outfits/wf-o004.png",   // 原图
  "items": [                  // 白底单品图列表
    { "id": "wf-o004-shorts", "name": "藏青短裤", "category": "裤子", "color": "藏青",
      "image": "/static/assets/outfits/wf-o004-shorts.png" }
  ],
  "dims": {                   // 六组维度,值均为数组(可多选)
    "outfitStyle": ["极简", "休闲"],   // 穿搭风格
    "category": ["裤子", "鞋"],        // 品类
    "color": ["藏青", "黑"],           // 颜色(平铺存值,分组在维度枚举里)
    "material": [],                    // 材质
    "fit": ["短款"],                   // 版型
    "func": []                         // 功能属性
  },
  "image": "...",             // 旧版兼容字段(cover 的同义字段,前端可忽略)
  "created_at": "2026-09-16 08:14:51"
}
```

详情相册图片顺序(约定):**原图 → 单品图 N 张 → 平铺总图**。

## 四、接口清单(10 个)

> ⚠️ 版本说明:本节 1~9 为早期 multipart 版本说明,部分路径/参数与当前后端不一致
> (当前列表为 `GET /api/outfit`、上传为 `POST /api/wellflow/image/uploads`、请求体均为 JSON + storage_uri),
> 前端以当前代码为准;**第 8、10 节(拆解 / 异步创建主流程)按当前实际契约编写**。

### 1. 列表(带筛选)
`GET /api/assets?category=outfit&scope=&q=&dims=`

| 参数 | 说明 |
|---|---|
| category | 固定传 `outfit` |
| scope | `official` / `mine` / 不传=全部 |
| q | 关键词:编号/名称/标签;支持自然语言多词(空格/逗号分隔,任一命中) |
| dims | JSON 字符串,如 `{"outfitStyle":["极简"],"color":["米白"]}` —— 组内多值 OR,组间 AND |

响应:`{ "code":0, "data": { "list":[...记录...], "total": 8, "stats": {各分类数量} } }`

### 2. 单条详情
`GET /api/assets/{asset_id}`,如 `/api/assets/WF-O004` → `{ "code":0, "data":{完整记录} }`,不存在 404

### 3. 维度枚举
`GET /api/outfit/dimensions` → `{ "code":0, "data": { "groups": { ... } } }`
六组:`category` 品类 / `outfitStyle` 穿搭风格 / `color` 颜色(**含分组**:`groups:[{label:"基础色",options:[...]},{label:"彩色",options:[...]}]`)/ `material` 材质 / `fit` 版型 / `func` 功能属性

### 4. 创建穿搭
`POST /api/outfit`(multipart/form-data)

| 字段 | 必填 | 说明 |
|---|---|---|
| name | ✓ | 名称 |
| desc | | 一句话描述(空则后端自动生成) |
| dims | | JSON 字符串,六组维度 |
| tags | | 逗号分隔(空则按 dims 自动生成) |
| origin | | `upload`/`url`/`demo`/`ai`,默认 upload |
| original_url | ✓ | 原图 URL(相对路径或 http) |
| cover_url | ✓ | 平铺总图 URL |
| items_json | ✓ | 单品数组 JSON:`[{"id":"a","name":"藏青短裤","category":"裤子","color":"藏青","image":"/static/..."}]` |

响应:`{ "code":0, "data":{新记录} }`(id 自动分配 WF-Oxxx)

### 5. 编辑穿搭(仅我的资产)
`PUT /api/outfit/{id}`(multipart):`name` / `desc` / `tags` / `dims`(JSON)/ `cover_url`(可选,更换平铺总图)
官方资产 → **HTTP 403**

### 6. 删除穿搭(仅我的资产)
`DELETE /api/outfit/{id}` → `{ "code":0, "data":{"deleted":"WF-O012"} }`
官方资产 → **HTTP 403**。前端对 scope=official 应隐藏编辑/删除按钮。

### 7. 图片上传(通用)
`POST /api/outfit/upload`(multipart:`file`)→ `{ "code":0, "data":{"url":"/static/assets/outfits/user_upload_xxx.png"} }`

### 8. AI 拆解(点击即入库,后台识别+抠图)
`POST /api/outfit/ai-extract`(JSON)

| 字段 | 必填 | 说明 |
|---|---|---|
| original_uri | ✓ | 原图 storage_uri(先走 `POST /api/wellflow/image/uploads` 统一上传) |
| session_id | | 可选 |
| mode | | `real`(默认)/ `demo`(预制示例) |

**立即响应(毫秒级,不等 AI)**:
```json
{ "code":0, "data": { "outfit_id":15, "outfit_no":"WF-O015", "task_id":"1789xxx_ab12cd", "status":"extracting" } }
```
**语义:行已入库,列表立即可见(拆解中);后台 VLM 识别单品 + 逐件抠图;**
完成 → 行补 items、status=pending_select(待选件);失败 → status=failed。

轮询 `GET /api/outfit/ai-status?task_id=xxx`(建议 2s 间隔):
```json
// 处理中:progress{done,total}=抠图进度,items 识别结果先行可见
{ "code":0, "data": { "task_type":"extract", "status":"processing", "progress":{"done":3,"total":6},
  "items":[...], "outfit_id":15, "error":"" } }
// done:行已是 pending_select,items 带 storage_uri
// failed:data.error=原因(前端提示删除重来)
```

**两种模式**:
- **real**:VLM(模型池)识别(≤6 件,**空清单自动重试一次**)→ 逐件抠图(gpt-image-2 降级链),约 1-3 分钟;部分单品失败不影响成功部分
- **demo**:预制 6 件示例,秒回,同样入库

### 9. 平铺总图合成
`POST /api/outfit/flatlay`(multipart:`items_json` = 已选单品数组,最多 6 件)
→ `{ "code":0, "data": { "flatlay_url":"/static/outfit_ai/flatlay_xxx.png" } }`(1448x1086,4:3)

### 10. 异步创建主流程(拆解 → 选件生成 → 确认入库)★ 当前主流程

**状态机(列表 status 字段,全程可见,用户任意时刻可离开可回来接上)**:
```
extracting(拆解中)→ pending_select(待选件)→ generating(生成中)→ pending_confirm(待确认)→ active
任一步失败 → failed(可删除重来)
```

**三步调用**:

**① 拆解** = 接口 8(`POST /api/outfit/ai-extract`)→ 行 extracting → 后台识别+抠图 → pending_select

**② 选件后生成** = `POST /api/outfit/generate`(JSON):

| 字段 | 必填 | 说明 |
|---|---|---|
| mode | | 固定传 `items` |
| original_uri | ✓ | 原图 storage_uri |
| items | ✓ | 用户勾选的单品数组(带 storage_uri) |
| outfit_id | ✓ | 拆解返回的 outfit_id(**更新该行,不新建**) |
| extra_context | | 可选,打标补充意图(透传 VLM) |

立即响应(毫秒级):`{ "outfit_id":15, "outfit_no":"WF-O015", "task_id":"...", "status":"generating" }`
后台:平铺总图 → VLM 打标+描述+建议名 → 行补 cover/dims/desc/tags/name、**status=pending_confirm**(预填值即确认页默认值);失败 → failed。
校验:行必须是 pending_select 才能生成(否则 400);official 行 403。

**③ 确认入库** = `PUT /api/outfit/{id}`(JSON):
用户改完提交:`name` / `desc` / `tags` / `dims`(预填 AI 值,可改)+ **`status:"active"`** → 正式入库

**轮询**:两个任务阶段共用 `GET /api/outfit/ai-status?task_id=xxx`:
- 拆解任务:`task_type:"extract"`,progress{done,total}=抠图进度
- 生成任务:`task_type:"generate"`,step=queued/extract/cutout/flatlay/tagging/saving
- 任务文件 status 三值:processing / done / failed

**两套 status 不要混淆**:

| 来源 | 字段 | 取值 | 含义 |
|---|---|---|---|
| DB 行(列表/详情) | status | extracting / pending_select / generating / pending_confirm / active / failed | 资产流程状态 |
| 任务文件(ai-status) | status | processing / done / failed | 后台任务是否干完 |

**契约约定**:
1. extracting/generating 行**禁编辑**(后台在写);pending_select 行只能「点进选件」;pending_confirm 行点进「确认编辑」;active 可编辑
2. extracting/generating 行 cover 为空,列表卡片显示占位图(可用 original_url)+ 状态徽标
3. **批量**:拆解/生成都可同时开多个,后端排队不拒绝(全局并发闸 3)
4. 服务重启后中断的任务自动标 failed(ai-status 或列表刷新时触发)
5. mode=auto(一键全自动)后端保留,**前端不挂入口**

## 五、创建穿搭标准调用时序

```
步骤1 提供图片
  ├─ 上传:先 POST /api/outfit/upload 拿 url(可选),或直接传文件给 ai-extract
  ├─ 直链:直接把链接传给 ai-extract 的 source_url
  └─ 示例照片:ai-extract 不传图
步骤2 拆解
  POST /api/outfit/ai-extract → 拿 task_id + original_url
  轮询 GET /api/outfit/ai-status → done 后渲染 6 件单品(默认全选,可取消)
  前端本地记录:已选单品 id 集合 + original_url
步骤3 确认入库
  POST /api/outfit/flatlay(items_json=已选单品)→ 拿 flatlay_url 预览
  前端自动生成名称(首件单品名+「搭配」)、预填 dims(品类/颜色取已选单品,
  outfitStyle 默认 休闲+极简)、描述(可编辑,规则同后端)
  返回上一步改选 → 重新调 flatlay 并重新预填
  确认 → POST /api/outfit(name/desc/dims/origin/original_url/cover_url/items_json)
        → 成功跳转列表(我的资产可见)
```

### ★ 异步创建主流程时序(当前主流程,用户全程零等待)

```
步骤1 上传原图
  POST /api/wellflow/image/uploads(session_id)→ 拿 original storage_uri
步骤2 点「拆解穿搭」
  POST /api/outfit/ai-extract(original_uri)→ 秒回 {outfit_id, task_id}
  列表出现新记录【拆解中】,用户可自由活动
步骤3 拆解完成 → 列表该记录变【待选件】,用户点进 → 勾选单品
步骤4 点「用这几件生成穿搭图」
  POST /api/outfit/generate(mode=items, original_uri, items, outfit_id)→ 秒回
  该记录变【生成中】
步骤5 生成完成 → 记录变【待确认】,用户点进 → 修改名称/标签/描述(预填 AI 值)
步骤6 点「确认入库」
  PUT /api/outfit/{id}(name/desc/tags/dims + status:"active")→ 正式入库
```

## 六、联调注意事项

1. **官方资产只读**:scope=official 的记录,前端隐藏编辑/删除入口;后端也会 403 兜底
2. **图片相对路径**:所有返回的图片路径都要拼 base;可直接 `<img src="{base}{url}">`
3. **演示拆解离线可用**:6 件示例单品图已内置(static/assets/outfit-demo/,随代码提交),拆解秒回、不依赖 AI 网关;仅当图片文件缺失时才会尝试调 AI 生成(需连通 `192.168.110.254`),失败则任务 failed,前端请展示 error 文案
4. **颜色维度是分组下拉**:枚举里 `color` 特殊,渲染时按 groups 分组显示
5. **参考实现**:后端仓库 `static/穿搭库.html`、`static/创建穿搭.html` 两个页面就是完整调用示例,前端可直接对照(含筛选、相册、三步流程的全部交互逻辑)
6. **联调数据**:库里已有 8 套官方穿搭(WF-O004~O011,含图),「我的资产」需要前端走创建流程生成

## 七、快速验证(后端自测用)

```bash
curl http://localhost:8000/api/outfit/dimensions
curl http://localhost:8000/api/assets/WF-O004
curl http://localhost:8000/api/assets?category=outfit&scope=official
curl -X DELETE http://localhost:8000/api/outfit/WF-O004   # 预期 403
```
