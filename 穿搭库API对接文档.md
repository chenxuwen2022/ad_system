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

## 四、接口清单(9 个)

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

### 8. AI 拆解(异步任务+轮询)
`POST /api/outfit/ai-extract`(multipart,三选一):
- `image`:上传的照片文件
- `source_url`:图片直链(http/https,仅可直开的图片)
- 都不传:使用内置示例照片
- `mode`(可选):`real` / `demo`;**不传则自动判定**:传了照片 → real 真实拆解;无图(示例照片)→ demo 演示模式

立即返回:
```json
{ "code":0, "data": { "task_id":"1726xxx_ab12cd", "original_url":"/static/outfit_ai/orig_xxx.png", "status":"processing" } }
```
轮询 `GET /api/outfit/ai-status?task_id=xxx`(建议 2s 间隔):
```json
// processing 时(真实模式):
{ "code":0, "data": { "status":"processing", "progress":{"done":3,"total":6}, "items":[...], "failed_items":[...] } }
// done 时:
{ "code":0, "data": { "status":"done", "items":[ {"id":"r1","name":"军绿色工装衬衫外套","category":"衬衫","color":"军绿","image":"/static/outfit_ai/item_xxx_0.png"}, ... ], "failed_items":[{"name":"...","error":"..."}], "progress":{"done":6,"total":6} } }
// failed 时:data.status="failed", data.error=原因(前端展示错误并给「重试」按钮,后端不会降级)
```

**两种模式**:
- **真实模式(mode=real,上传照片自动触发)**:VLM(gemini-3.7-flash)识别照片单品清单(≤6 件)→ 逐件生成白底图(gpt-image-2 优先,降级 gpt-image-2.5-flare → mai-image-2.5),约 1-3 分钟;识别结果先行返回,前端可用 `progress` 展示「识别到 N 件,抠图中 x/N」;部分单品失败不影响成功部分(见 `failed_items`)
- **演示模式(mode=demo,示例照片)**:返回 6 件预制示例单品,秒回

### 9. 平铺总图合成
`POST /api/outfit/flatlay`(multipart:`items_json` = 已选单品数组,最多 6 件)
→ `{ "code":0, "data": { "flatlay_url":"/static/outfit_ai/flatlay_xxx.png" } }`(1448x1086,4:3)

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
