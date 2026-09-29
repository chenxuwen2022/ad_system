import os
from datetime import datetime
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from ad.models import AdLaunchRequest, AdLaunchResult
from ad.douyin_api import DouYinAdService
from ad.file_service import delete_media_file
from ad.token_manager import get_token_mgr
from ad.config import DOUYIN_CONFIG
from ad.db import SessionLocal, AdvertiserDB, MaterialTagDB, MaterialMarkDB, MaterialLaunchDB, MaterialCategoryDB, MaterialBizStatusDB, SkuDB, MATERIAL_BIZ_STATUSES
from sqlalchemy import text

router = APIRouter()


class AuthCodeBody(BaseModel):
    auth_code: str


class AdvertiserAccountBody(BaseModel):
    advertiser_id: str
    name: str = ""
    id: Optional[int] = None  # 编辑时传数据库主键，用于精确更新，避免误判为新增


class MaterialTagBody(BaseModel):
    name: str
    color: str = "#1f6feb"
    id: Optional[int] = None  # 编辑时传数据库主键，用于精确更新
    tag_type: str = "public"  # public=公共标签 / personal=个人标签


class AiContextBody(BaseModel):
    advertiser_id: str = ""
    links: list = []          # 竞品链接列表
    market_data: str = ""     # 行业市场数据文本


def _ai_context_path(aid):
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"ai_context_{aid}.json")


def _resolve_advertiser_id(req_id, accounts):
    """确定投放使用的广告主ID：请求指定 > 配置文件默认 > 唯一账户"""
    ids = [str(a.get("advertiser_id")) for a in accounts]
    if req_id:
        return str(req_id), None
    # 配置文件里指定了默认千川投放账户，直接用（千川账户不在 oauth2 账户列表里）
    default_id = str(DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID") or "")
    if default_id:
        return default_id, None
    if not ids:
        return None, "当前token未授权任何广告主，请先通过 /api/token/fetch 完成授权"
    if len(accounts) == 1:
        return ids[0], None
    return None, f"当前token授权了多个广告主({ids})，请在请求中指定 advertiser_id"


@router.post("/api/token/fetch")
async def fetch_token(body: AuthCodeBody):
    try:
        tm = get_token_mgr()
        advertiser_ids = tm.request_first_token(body.auth_code)
        return {
            "ok": True,
            "msg": "token获取并持久化成功",
            "advertiser_ids": advertiser_ids,
            "access_token": tm.get_access_token()[:40] + "...",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _save_material_marks(file_path: str, tags):
    """投放后给素材打标签标记：按文件路径唯一存储，多标签逗号分隔"""
    names = [t.strip() for t in (tags or []) if t and t.strip()]
    if not names or not file_path:
        return
    db = SessionLocal()
    try:
        row = db.query(MaterialMarkDB).filter(MaterialMarkDB.file_path == file_path).first()
        if row:
            row.tags = ",".join(names)
            row.update_time = datetime.now()
        else:
            row = MaterialMarkDB(file_path=file_path, tags=",".join(names))
            db.add(row)
        db.commit()
    finally:
        db.close()


def _save_launch_record(file_path: str, status: str, mode: str,
                        plan_id: str = "", plan_name: str = "",
                        product_id: str = "", detail: str = "",
                        advertiser_id: str = "", budget: float = 0,
                        biz_status: str = "", material_id: str = ""):
    """记录一次素材投放历史，用于素材库展示投放状态。
    biz_status：业务状态（待审核/审核驳回/通过-待投放/直播间已投放/商城已投放/已投放商品+直播间/放弃测试）。
    SQLite material_launch（现有功能）+ PostgreSQL launch_record（投放记录表）双写。"""
    if not file_path:
        return
    db = SessionLocal()
    try:
        db.add(MaterialLaunchDB(
            file_path=file_path, status=status, mode=mode,
            biz_status=biz_status or "",
            plan_id=plan_id or "", plan_name=plan_name or "",
            product_id=product_id or "", detail=(detail or "")[:300],
            material_id=material_id or "", advertiser_id=advertiser_id or "",
        ))
        db.commit()
    finally:
        db.close()
    # 同步写 PostgreSQL 投放记录表（PG 不可用时静默跳过，不影响主流程）
    try:
        from ad.pg_db import save_launch_record
        save_launch_record(
            advertiser_id=advertiser_id or "", file_path=file_path,
            status=status, mode=mode, plan_id=plan_id or "", plan_name=plan_name or "",
            product_id=product_id or "", budget=float(budget or 0),
            detail=(detail or "")[:1000],
        )
    except Exception:
        pass


@router.get("/api/advertisers")
async def get_advertisers():
    """用库里的token实时查询已授权的广告主账户列表"""
    try:
        tm = get_token_mgr()
        items = tm.get_advertiser_ids()
        return {"success": True, "data": items}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/ad/launch")
async def ad_launch(req: AdLaunchRequest):
    if req.platform == "douyin":
        try:
            advertiser_id = req.advertiser_id
            if not advertiser_id:
                # 后台自动查询当前token已授权的广告主
                tm = get_token_mgr()
                accounts = tm.get_advertiser_ids()
                advertiser_id, err = _resolve_advertiser_id(None, accounts)
                if err:
                    return {"success": False, "error": err}
            ad_service = DouYinAdService(advertiser_id=advertiser_id)
        except Exception as e:
            return {"success": False, "error": str(e)}
        f_path = req.local_file_path

        # ===== 测试模式：不调用任何千川真实接口，不依赖本地文件，本地模拟整条投放链路 =====
        if req.test_mode:
            base_name = f_path.replace("\\", "/").split("/")[-1]
            file_stem = base_name.rsplit(".", 1)[0] if "." in base_name else (base_name or "测试素材")
            suffix = base_name.rsplit(".", 1)[-1].lower() if "." in base_name else ""
            f_type = "video" if suffix in ["mp4", "mov"] else "image"
            prefix = DOUYIN_CONFIG.get("CREATIVE_PREFIX", "商城")
            creative_name = req.creative_name or f"{prefix}{file_stem}"
            base_plan_name = req.ad_plan_name or DOUYIN_CONFIG.get("DEFAULT_PLAN_NAME", "商城")
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            plan_name = f"{base_plan_name}_{file_stem}_{datetime.now().strftime('%H%M%S')}"[:80]
            group_name = req.ad_group_name or DOUYIN_CONFIG.get("DEFAULT_GROUP_NAME", "商城")
            budget = req.budget if req.budget is not None else DOUYIN_CONFIG.get("DEFAULT_BUDGET", 100)
            bid = req.bid if req.bid is not None else DOUYIN_CONFIG.get("DEFAULT_BID", 1)
            chosen_products = req.product_ids or DOUYIN_CONFIG.get("PRODUCT_IDS") or []
            chosen_pid = str(chosen_products[0]) if chosen_products else None
            plan_ref = req.plan_id or "（未选择）"
            steps = [
                f"① 本地文件校验：通过（测试模式不校验素材有效性，文件名={base_name or '（未指定）'}）",
                f"② 参数组装：计划名={plan_name}　广告组={group_name}　创意名={creative_name}",
                f"③ 投放配置：预算={budget}元　出价={bid}元　商品ID={chosen_pid or '未选择'}　抖音号={req.aweme_id or 'config默认'}",
                f"④ 投放计划：素材将追加到所选计划【{plan_ref}】下，不新建计划（测试模式仅模拟）",
                "⑤ 千川素材上传：跳过（测试模式）",
                "⑥ 千川追加素材：跳过（测试模式）",
                "⑦ 模拟返回：已生成测试素材ID（不会真实创建、不产生费用）",
                f"⑧ 素材标记：{('、'.join(req.tags) + '（已保存到素材标记）') if req.tags else '未选择标签'}",
            ]
            _save_material_marks(f_path, req.tags)
            _save_launch_record(f_path, "success", "test",
                                plan_id=req.plan_id or "", plan_name=plan_ref,
                                product_id=chosen_pid or "",
                                detail="测试模式模拟投放",
                                advertiser_id=advertiser_id or "", budget=budget,
                                biz_status="通过-待投放")
            return {
                "success": True,
                "test_mode": True,
                "advertiser_id": advertiser_id,
                "local_file_path": f_path,
                "material_id": f"TEST-MAT-{stamp}",
                "ad_group_id": f"TEST-GROUP-{stamp}",
                "ad_plan_id": req.plan_id or f"TEST-PLAN-{stamp}",
                "audit_status": "TESTING",
                "tags": req.tags or [],
                "steps": steps,
                "error_msg": "测试模式：未调用千川真实接口，未创建真实计划，未产生任何费用。",
            }

        # ===== 必须投放到用户选择的计划下，不再新建计划 =====
        if not req.plan_id:
            return AdLaunchResult(
                success=False,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                error_msg="未选择投放计划：素材必须投放到指定的投放计划下，系统不再自动新建计划，请先在页面选择投放计划",
            )

        if not os.path.exists(f_path):
            return AdLaunchResult(
                success=False,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                error_msg="素材文件已被清理，请重新上传图片/视频后再点投放",
            )

        try:
            new_ad_id, new_material_id = ad_service.add_video_to_plan(req.plan_id, f_path)
            result = AdLaunchResult(
                success=True,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                ad_plan_id=new_ad_id,
                material_id=new_material_id,
                audit_status="PENDING",
                error_msg=f"已把素材追加到所选投放计划{req.plan_id}下（未新建计划）",
            )
        except Exception as e:
            result = AdLaunchResult(
                success=False,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                error_msg=f"追加素材到计划{req.plan_id}失败：{e}",
            )
        if result.success:
            _save_material_marks(req.local_file_path, req.tags)
            _save_launch_record(req.local_file_path, "success", "real",
                                plan_id=req.plan_id, plan_name=req.plan_name or req.plan_id or "", product_id=",".join(req.product_ids or []),
                                detail="已追加到投放计划",
                                advertiser_id=advertiser_id or "", budget=req.budget or 0,
                                biz_status="待审核",
                                material_id=getattr(result, 'material_id', '') or '')
            # delete_media_file(req.local_file_path)  # 保留本地文件
        else:
            _save_launch_record(req.local_file_path, "fail", "real",
                                plan_id=req.plan_id or "", plan_name=req.plan_name or req.plan_id or "", product_id=",".join(req.product_ids or []),
                                detail=result.error_msg or "投放失败",
                                advertiser_id=advertiser_id or "", budget=req.budget or 0)
        return result

    elif req.platform in ["jd", "taobao"]:
        return {"success": False, "msg": "京东淘宝待实现，骨架已预留"}


class BatchLaunchRequest(BaseModel):
    plan_id: str = ""
    plan_name: str = ""
    file_paths: list = []          # 批量投放的素材本地路径
    product_ids: list = []         # 投放商品（取第一个作为商品卡商品）
    advertiser_id: str = ""
    tags: list = []
    budget: float = 0


@router.post("/api/ad/launch_batch")
async def ad_launch_batch(req: BatchLaunchRequest):
    """批量投放素材到同一计划。

    - 图片：多张图片以"图文（多图轮播）"方式追加到同一计划（千川自选图片每创意只能1张，
      多图必须用 carousel 图文轮播），一次调用全部图片进同一个图文。
    - 视频：逐个追加（视频不受单素材限制）。
    """
    from ad.services.douyin_api import DouYinAdService as _DS
    advertiser_id = req.advertiser_id or str(DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID") or "")
    if not advertiser_id:
        return {"success": False, "error_msg": "未指定广告主 advertiser_id"}
    if not req.plan_id:
        return {"success": False, "error_msg": "未选择投放计划"}
    if not req.file_paths:
        return {"success": False, "error_msg": "未选择任何素材"}
    paths = [p for p in req.file_paths if os.path.exists(p)]
    missing = [p for p in req.file_paths if not os.path.exists(p)]
    if not paths:
        return {"success": False, "error_msg": "素材文件均已不存在，请重新上传后再投放"}

    ad_service = _DS(advertiser_id=advertiser_id)
    product_id = (req.product_ids or [""])[0]
    results = []
    images = [p for p in paths if p.rsplit(".", 1)[-1].lower() in ["jpg", "jpeg", "png", "bmp", "webp"]]
    videos = [p for p in paths if p.rsplit(".", 1)[-1].lower() in ["mp4", "mpeg", "3gp", "avi", "mov"]]

    try:
        if images:
            if not product_id:
                return {"success": False, "error_msg": "图片投放需要指定商品 product_id（商品卡方图必选）"}
            carousel_ret = ad_service.add_carousel_to_plan(req.plan_id, images, product_id)
            carousel_id = carousel_ret["carousel_id"] if isinstance(carousel_ret, dict) else carousel_ret
            carousel_img_ids = carousel_ret.get("image_ids", []) if isinstance(carousel_ret, dict) else []
            results.append({"type": "image", "count": len(images), "carousel_id": carousel_id})
            for p in images:
                _save_launch_record(p, "success", "real",
                                    plan_id=req.plan_id, plan_name=req.plan_name or req.plan_id or "",
                                    product_id=str(product_id),
                                    detail=f"已追加到投放计划（图文轮播 carousel_id={carousel_id}）",
                                    advertiser_id=advertiser_id or "", budget=req.budget or 0,
                                    biz_status="待审核",
                                    material_id=f"carousel:{carousel_id}:{','.join(carousel_img_ids)}")
        if videos:
            for v in videos:
                try:
                    new_material_id = ad_service.add_video_material_to_plan(req.plan_id, v, req.product_ids or None)
                    results.append({"type": "video", "file": os.path.basename(v), "material_id": new_material_id})
                    _save_launch_record(v, "success", "real",
                                        plan_id=req.plan_id, plan_name=req.plan_name or req.plan_id or "",
                                        product_id=",".join(req.product_ids or []),
                                        detail="已追加到投放计划",
                                        advertiser_id=advertiser_id or "", budget=req.budget or 0,
                                        biz_status="待审核",
                                        material_id=new_material_id)
                except Exception as e:
                    results.append({"type": "video", "file": os.path.basename(v), "success": False, "error_msg": str(e)})
                    _save_launch_record(v, "fail", "real",
                                        plan_id=req.plan_id or "", plan_name=req.plan_name or req.plan_id or "",
                                        product_id=",".join(req.product_ids or []),
                                        detail=str(e), advertiser_id=advertiser_id or "", budget=req.budget or 0)
        if req.tags:
            for p in paths:
                _save_material_marks(p, req.tags)
        return {
            "success": True,
            "results": results,
            "total": len(paths),
            "missing": missing,
            "error_msg": f"共投放 {len(paths)} 个素材" + (f"；{len(missing)} 个文件已不存在" if missing else ""),
        }
    except Exception as e:
        return {"success": False, "error_msg": f"批量投放失败：{e}"}


@router.post("/api/ai_analysis")
async def ai_analysis():
    """汇总本账户所有图片/视频素材的投放数据，并用 DeepSeek 分析问题与改进建议。"""
    tm = get_token_mgr()
    advertiser_id = DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
    ad_service = DouYinAdService(advertiser_id=advertiser_id)
    try:
        data = ad_service.ai_analyze_materials()
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "materials": [], "ai": f"分析失败：{e}"}


@router.get("/api/material_list")
async def material_list(advertiser_id: str = "", refresh: bool = False):
    """返回全部素材（供页面下拉框），按消耗降序。可指定 advertiser_id 查询对应账户的素材。
    refresh=true 时强制绕过缓存，从千川重新拉取最新数据（约40秒）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        mats = svc.get_all_materials_report(force=refresh)[0]["materials"]
        items = [{"id": m["id"], "name": m["name"], "type": m["type"],
                  "消耗": m["消耗"], "成交金额": m["成交金额"], "支付ROI": m["支付ROI"]}
                 for m in mats]
        return {"success": True, "data": items, "total": len(items)}
    except Exception as e:
        return {"success": False, "error": str(e), "data": []}


@router.get("/api/materials_by_product")
async def materials_by_product(advertiser_id: str = "", product_id: str = ""):
    """返回指定商品下的素材列表（来自该商品在投全域计划挂载的素材，去重），
    并聚合该商品所有素材的投放数据（素材数/消耗/净成交/总成交/ROI/成交单数）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        if not product_id:
            return {"success": True, "data": [], "agg": None, "total": 0, "note": "未选择商品"}
        mat_ids = set(svc.get_product_material_ids(product_id))
        mats = svc.get_all_materials_report()[0]["materials"]
        items = [m for m in mats if m["id"] in mat_ids]
        agg = {"素材数": len(items),
               "消耗": round(sum(float(m.get("消耗") or 0) for m in items), 2),
               "成交金额": round(sum(float(m.get("成交金额") or 0) for m in items), 2),
               "成交单数": int(sum(float(m.get("成交单数") or 0) for m in items)),
               "净成交金额": round(sum(float(
                   next((x["value"] for x in (m.get("metrics_all") or [])
                        if x["field"] == "total_order_settle_amount_for_roi2_1h"), 0))
                   for m in items), 2)}
        if agg["消耗"]:
            agg["支付ROI"] = round(agg["成交金额"] / agg["消耗"], 2)
        out = [{"id": m["id"], "name": m["name"], "type": m["type"],
                "消耗": m["消耗"], "成交金额": m["成交金额"], "支付ROI": m["支付ROI"]}
               for m in items]
        out.sort(key=lambda x: -float(x["消耗"] or 0))
        return {"success": True, "data": out, "agg": agg, "total": len(out)}
    except Exception as e:
        return {"success": False, "error": str(e), "data": [], "agg": None}


@router.get("/api/material_detail")
async def material_detail(material_id: str, advertiser_id: str = ""):
    """单个素材：汇总+逐日曲线+DeepSeek点评与建议。可指定 advertiser_id 查询对应账户的素材。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_material_detail(material_id)
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/material_detail_by_name")
async def material_detail_by_name(name: str = "", advertiser_id: str = ""):
    """按素材文件名匹配千川报表，返回投放汇总 + 逐日曲线 + AI 点评（素材详情页使用）。
    报表素材行按名称维度（roi2_material_video_name / roi2_material_image_name）匹配，
    支持 文件名带扩展名 / 不带扩展名 两种写法。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        mats = svc.get_all_materials_report()[0]["materials"]
        key = (name or "").strip()
        if not key:
            return {"success": False, "error": "缺少素材名称"}
        base = key.rsplit(".", 1)[0].strip() if "." in key else key
        # 仅精确匹配（文件名去扩展名 == 报表名称去扩展名），避免子串模糊匹配误配到其他素材
        row = None
        for m in mats:
            nm = str(m.get("name") or "").strip()
            nb = nm.rsplit(".", 1)[0].strip() if "." in nm else nm
            if nb == base:
                row = m
                break
        if not row:
            return {"success": False, "error": "素材「%s」近3个月无投放数据" % key}
        data = svc.get_material_detail(str(row["id"]))
        # 平铺 summary 到顶层，前端直接读 消耗/成交金额/点击率/转化率/支付ROI
        return {"success": True, "material_id": str(row["id"]), **data.get("summary", {}),
                "daily": data.get("daily", []), "ai": data.get("ai", ""),
                "preview": data.get("preview", {})}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/material_detail_by_id")
async def material_detail_by_id(material_id: str = "", advertiser_id: str = "", name: str = ""):
    """按千川素材ID查询报表汇总（精确匹配，数据与巨量后台一致）。
    兼容三种 ID 写法：
    - 纯数字 material_id（报表/素材库用）：直接匹配报表行
    - v 开头 video_id（投放计划详情用）：经 video_id → material_id 映射转换后匹配
    - carousel:<cid>:<imgid1,imgid2...>（图文轮播）：按图片素材ID 逐行匹配并汇总指标
    查不到时可用 name 参数按文件名回退匹配。
    """
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        mats = svc.get_all_materials_report()[0]["materials"]
        mid = (material_id or "").strip()
        if not mid:
            return {"success": False, "error": "缺少素材ID"}
        img_ids = []
        # 图文轮播格式 carousel:<cid>:<imgid1,imgid2...>；老数据无图片ID 时通过 carousel/get 取图片素材ID
        if mid.startswith("carousel:"):
            parts = mid.split(":")
            if len(parts) >= 3:
                img_ids = [x for x in parts[2].split(",") if x]
            else:
                img_ids = svc.get_carousel_image_ids(parts[1])
        # v 开头 video_id → 数字 material_id
        elif mid.startswith("v"):
            vmap = svc._build_video_id_map()
            mid = vmap.get(mid) or mid
        # 多图汇总：逐行匹配报表，指标加总重算率值
        if img_ids:
            rows = [m for m in mats if str(m["id"]) in img_ids]
            if not rows:
                # 图片ID未命中报表，回退按文件名匹配
                if name:
                    base = (name or "").strip().rsplit(".", 1)[0]
                    rows = [m for m in mats if str(m.get("name") or "").strip().rsplit(".", 1)[0] == base]
                if not rows:
                    return {"success": False, "error": "素材 %s 近3个月无投放数据" % material_id}
            agg = {
                "type": "图片", "id": material_id,
                "material_id": ",".join(str(m["id"]) for m in rows), "name": rows[0]["name"],
                "消耗": round(sum(r["消耗"] for r in rows), 2),
                "展示": sum(r["展示"] for r in rows),
                "点击": sum(r["点击"] for r in rows),
                "成单数": sum(r["成单数"] for r in rows),
                "成交金额": round(sum(r["成交金额"] for r in rows), 2),
                "退款率(%)": 0.0, "metrics_all": [],
            }
            _show = agg["展示"]; _click = agg["点击"]; _cost = agg["消耗"]; _gmv = agg["成交金额"]
            agg["点击率"] = round(_click / _show * 100, 2) if _show else 0.0
            agg["转化率"] = round(agg["成单数"] / _click * 100, 2) if _click else 0.0
            agg["支付ROI"] = round(_gmv / _cost, 2) if _cost else 0.0
            agg["净成交ROI"] = agg["支付ROI"]
            return {"success": True, **agg, "daily": [], "ai": "", "preview": {}}
        # 单素材：精确匹配报表行
        if not mid:
            return {"success": False, "error": "素材ID为空"}
        row = next((m for m in mats if str(m["id"]) == str(mid)), None)
        if not row and name:
            base = (name or "").strip().rsplit(".", 1)[0]
            row = next((m for m in mats if str(m.get("name") or "").strip().rsplit(".", 1)[0] == base), None)
        if not row:
            return {"success": False, "error": "素材 %s 近3个月无投放数据" % material_id}
        data = svc.get_material_detail(str(row["id"]))
        # 平铺 summary 到顶层，前端直接读 消耗/成交金额/点击率/转化率/支付ROI
        return {"success": True, "material_id": str(row["id"]), **data.get("summary", {}),
                "daily": data.get("daily", []), "ai": data.get("ai", ""),
                "preview": data.get("preview", {})}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/ai_context")
async def save_ai_context(body: AiContextBody):
    """保存 AI 分析上下文：竞品链接列表 + 行业市场数据（按广告主分文件存储）。"""
    import json
    aid = str(body.advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID"))
    links = [str(x).strip() for x in (body.links or []) if str(x).strip()]
    market = (body.market_data or "").strip()
    try:
        with open(_ai_context_path(aid), "w", encoding="utf-8") as f:
            json.dump({"links": links, "market_data": market,
                       "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
        return {"success": True, "links": links, "market_data_len": len(market)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/ai_context")
async def get_ai_context(advertiser_id: str = ""):
    """读取已保存的 AI 分析上下文。"""
    import json
    aid = str(advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID"))
    path = _ai_context_path(aid)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return {"success": True, "links": d.get("links", []),
                    "market_data": d.get("market_data", ""), "updated": d.get("updated", "")}
        return {"success": True, "links": [], "market_data": "", "updated": ""}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/material_multi_shop")
async def material_multi_shop(material_id: str, advertiser_id: str = ""):
    """同一素材跨店铺分析：
    1) 主店铺（当前选中店铺）：素材集合数据（该店铺全部素材聚合）+ 该素材具体数据
    2) 店铺对比：遍历后台保存的全部店铺，查询同一素材在各店铺的 消耗/净成交金额/总成交金额/ROI/环比/同比
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    aid = str(advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID"))

    def _shop_row(a):
        """单店铺（轻量）：报表缓存行 + 近60天趋势，不拉90天逐日/AI/全量指标，大幅提速"""
        try:
            svc = DouYinAdService(advertiser_id=str(a["advertiser_id"]))
            mats = svc.get_all_materials_report()[0]["materials"]
            r = next((m for m in mats if m["id"] == str(material_id)), None)
            if r is None:
                return {"advertiser_id": str(a["advertiser_id"]), "name": a.get("name") or str(a["advertiser_id"]),
                        "ok": False, "error": "该素材近3个月无投放数据"}
            daily = svc._fetch_material_daily(material_id, r, days=60)
            recent7 = sum(x["cost"] for x in daily[-7:])
            prev7 = sum(x["cost"] for x in daily[-14:-7]) if len(daily) >= 7 else 0
            trend = ("上升" if recent7 > prev7 * 1.15
                     else ("下滑" if prev7 and recent7 < prev7 * 0.85 else "平稳")) if prev7 else "—"
            r30 = sum(x["cost"] for x in daily[-30:])
            p30 = sum(x["cost"] for x in daily[-60:-30]) if len(daily) >= 60 else 0
            yoy = ("上升" if p30 and r30 > p30 * 1.15
                   else ("下滑" if p30 and r30 < p30 * 0.85
                         else ("平稳" if p30 else "数据不足")))
            net = next((x["value"] for x in (r.get("metrics_all") or [])
                        if x["field"] == "total_order_settle_amount_for_roi2_1h"), 0)
            row = {
                "advertiser_id": str(a["advertiser_id"]), "name": a.get("name") or str(a["advertiser_id"]),
                "material_id": material_id, "素材名称": r.get("name", ""),
                "ok": True,
                "消耗": r.get("消耗", 0), "成交金额": r.get("成交金额", 0),
                "净成交金额": net, "支付ROI": r.get("支付ROI", 0),
                "成交单数": r.get("成交单数", 0), "trend": trend,
                "recent7_cost": round(recent7, 2), "prev7_cost": round(prev7, 2),
                "yoy_trend": yoy,
                "recent30_cost": round(r30, 2), "prev30_cost": round(p30, 2),
            }
            # 集合数据：该店铺全部素材聚合（消耗/成交/净成交/ROI 加权）
            agg = {"消耗": 0, "成交金额": 0, "净成交金额": 0, "成交单数": 0, "素材数": 0}
            try:
                mats = svc.get_all_materials_report()[0]["materials"]
                agg["素材数"] = len(mats)
                agg["消耗"] = round(sum(float(m.get("消耗") or 0) for m in mats), 2)
                agg["成交金额"] = round(sum(float(m.get("成交金额") or 0) for m in mats), 2)
                agg["成交单数"] = int(sum(float(m.get("成交单数") or 0) for m in mats))
                agg["净成交金额"] = round(sum(float(
                    next((x["value"] for x in (m.get("metrics_all") or [])
                         if x["field"] == "total_order_settle_amount_for_roi2_1h"), 0))
                    for m in mats), 2)
                if agg["消耗"]:
                    agg["支付ROI"] = round(agg["成交金额"] / agg["消耗"], 2)
                # 该素材在集合中的占比
                if agg["消耗"]:
                    row["消耗占比"] = round(float(row["消耗"]) / agg["消耗"] * 100, 1)
            except Exception:
                pass
            row["集合"] = agg
            return row
        except Exception as e:
            return {"advertiser_id": str(a["advertiser_id"]), "name": a.get("name") or str(a["advertiser_id"]),
                    "ok": False, "error": str(e)}

    try:
        # 主店铺详情（含 AI 点评，走原接口逻辑）
        main_svc = DouYinAdService(advertiser_id=aid)
        main_detail = main_svc.get_material_detail(material_id)

        # 店铺列表：后台保存的全部账户 + 主店铺兜底
        db = SessionLocal()
        try:
            accounts = [{"advertiser_id": r.advertiser_id, "name": r.name} for r in
                        db.query(AdvertiserDB).order_by(AdvertiserDB.id).all()]
        finally:
            db.close()
        if not any(str(a["advertiser_id"]) == aid for a in accounts):
            accounts.insert(0, {"advertiser_id": aid, "name": aid})

        shops = []
        with ThreadPoolExecutor(max_workers=min(4, len(accounts))) as ex:
            futs = {ex.submit(_shop_row, a): a for a in accounts}
            for fu in as_completed(futs):
                shops.append(fu.result())
        shops.sort(key=lambda x: (not x.get("ok"), -float(x.get("消耗") or 0)))

        main_s = main_detail.get("summary", {})
        return {"success": True,
                "main_shop": {"advertiser_id": aid, "summary": main_s},
                "shops": shops}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/material_export")
async def material_export(material_id: str, advertiser_id: str = ""):
    """单个素材全量数据导出：素材库信息+汇总+逐日曲线+关联计划/商品+AI点评，返回完整 JSON。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_material_full_data(material_id)
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/product_style")
async def product_style(product_id: str, advertiser_id: str = "", refresh: bool = False):
    """AI 识别商品主图/商详图样式（场景图/模特图/白底图/其他）。
    每张图结果磁盘缓存 7 天；refresh=true 强制重新识别。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.classify_product_images(product_id, force=bool(refresh))
        return data
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/material_style")
async def material_style(material_id: str, mtype: str = "视频", advertiser_id: str = "", refresh: bool = False):
    """AI 识图打标：判断素材属于 场景图/模特图/白底图/其他（素材库无此字段，用 VLM 识别）。
    material_id + mtype 定位素材（mtype: 视频/图片/标题），结果磁盘缓存 7 天。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.classify_material_style(material_id, mtype=mtype, force=bool(refresh))
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/audience_profile")
async def audience_profile(img_url: str, label: str = "素材图", advertiser_id: str = "", refresh: bool = False):
    """AI 受众画像：对单张图（投流素材图/商品主图/商详图）推断目标受众画像。
    输出 性别/年龄段/地域/兴趣标签/消费场景/一句话画像，磁盘缓存 7 天。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.classify_image_audience(img_url, label=label, force=bool(refresh))
        return data
    except Exception as e:
        return {"success": False, "label": label, "error": str(e)}


@router.get("/api/product_audience")
async def product_audience(product_id: str, advertiser_id: str = "", refresh: bool = False):
    """AI 受众画像：对商品主图 + 商详图逐张分析（去重，最多 5 张）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.classify_product_audience(product_id, force=bool(refresh))
        return data
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/ad/report")
async def get_report(advertiser_id: str, creative_id: str):
    ad_service = DouYinAdService(advertiser_id=advertiser_id)
    report_list = ad_service.get_ad_report_data(creative_id=creative_id)
    return {"data": [item.model_dump() for item in report_list]}


@router.get("/api/products")
async def list_products(advertiser_id: str = "", refresh: bool = False):
    """返回可投商品，并标注每个商品当前已关联的素材数。可指定 advertiser_id 查询对应账户的商品。
    refresh=true 时强制绕过缓存，从千川重新拉取（约10-30秒）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        # 商品列表与商品素材映射并行拉取（避免串行累加耗时）
        def _load_products():
            return svc.get_available_products(force=refresh)

        def _load_pmap():
            return svc.get_products_material_map(force=refresh)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_load_products)
            f2 = ex.submit(_load_pmap)
            products = f1.result()
            pmap = f2.result()
        for p in products:
            mids = pmap.get(str(p["id"]), [])
            p["material_count"] = len(mids)
            p["has_materials"] = len(mids) > 0
        return {"success": True, "data": products}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/plan_products")
async def plan_products(advertiser_id: str = "", plan_id: str = ""):
    """按计划ID返回该计划关联的商品列表（从计划详情取 product_id 后匹配可投商品）。"""
    try:
        if not plan_id:
            return {"success": False, "error": "缺少 plan_id 参数"}
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        products = svc.get_plan_products(plan_id)
        return {"success": True, "data": products}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/product/detail")
async def product_detail(advertiser_id: str = "", product_id: str = "", refresh: bool = False):
    """返回单个商品的完整信息：主图、商详图、价格、库存、销量等。
    从商品列表缓存中取（秒级），refresh=true 强制从千川重拉。"""
    try:
        if not product_id:
            return {"success": False, "error": "缺少 product_id 参数"}
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        products = svc.get_available_products(force=refresh)
        for p in products:
            if str(p.get("id")) == str(product_id):
                # 售价单位：分 → 元
                p = dict(p)
                if isinstance(p.get("discount_price"), (int, float)) and p["discount_price"] > 1000:
                    p["discount_price_yuan"] = round(p["discount_price"] / 100, 2)
                else:
                    p["discount_price_yuan"] = p.get("discount_price")
                return {"success": True, "data": p}
        return {"success": False, "error": f"未找到商品 {product_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/dmp/audiences")
async def dmp_audiences(advertiser_id: str = "", refresh: bool = False):
    """DMP 人群包列表：平台精选 + 自定义。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_dmp_audiences(force=refresh)
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/scene/report")
async def scene_report(advertiser_id: str = "", days: int = 7, refresh: bool = False):
    """场景维度投放报表（消耗/订单/GMV 按营销场景分布）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_scene_report(days=days, force=refresh)
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/plans")
async def list_plans(advertiser_id: str = "", refresh: bool = False):
    """返回指定店铺（广告主）下的全部投放计划下拉数据 + 预算汇总。
    含：plan_count、plans[]（ad_id/名称/状态/日预算/消耗）、
    total_budget（投放总量）、total_cost（已消耗）、remain_budget（剩余投放量）。
    refresh=true 时强制绕过缓存，从千川重新拉取（约10-30秒）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_all_plans_summary(force=refresh)
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------- 设置：广告主账户管理 ----------------
@router.get("/api/advertiser_accounts")
async def list_advertiser_accounts():
    db = SessionLocal()
    try:
        rows = db.query(AdvertiserDB).order_by(AdvertiserDB.id).all()
        return {"success": True, "data": [
            {"id": r.id, "advertiser_id": r.advertiser_id, "name": r.name} for r in rows
        ]}
    finally:
        db.close()


@router.post("/api/advertiser_accounts")
async def save_advertiser_account(body: AdvertiserAccountBody):
    """新增或修改广告主账户。
    - 带 id：按主键精确更新该记录（前端编辑态使用），避免 ID 输入框被改动后误判为新增
    - 不带 id：按 advertiser_id 查重，存在则更新、不存在则新增
    """
    aid = (body.advertiser_id or "").strip()
    name = (body.name or "").strip()
    if not aid:
        return {"success": False, "error": "广告主ID不能为空"}
    db = SessionLocal()
    try:
        if body.id is not None:
            row = db.query(AdvertiserDB).filter(AdvertiserDB.id == body.id).first()
            if row:
                conflict = (db.query(AdvertiserDB)
                            .filter(AdvertiserDB.advertiser_id == aid, AdvertiserDB.id != body.id)
                            .first())
                if conflict:
                    return {"success": False, "error": f"广告主ID {aid} 已被其它账户使用，请检查后重试"}
                row.advertiser_id = aid
                row.name = name
                row.update_time = datetime.now()
                db.commit()
                return {"success": True, "data": {"id": row.id, "advertiser_id": aid, "name": name}}
            # 指定的主键不存在，回落为按 advertiser_id 新增/更新
        row = db.query(AdvertiserDB).filter(AdvertiserDB.advertiser_id == aid).first()
        if row:
            row.name = name
            row.update_time = datetime.now()
        else:
            row = AdvertiserDB(advertiser_id=aid, name=name)
            db.add(row)
        db.commit()
        return {"success": True, "data": {"id": row.id, "advertiser_id": aid, "name": name}}
    finally:
        db.close()


@router.delete("/api/advertiser_accounts/{row_id}")
async def delete_advertiser_account(row_id: int):
    db = SessionLocal()
    try:
        row = db.query(AdvertiserDB).filter(AdvertiserDB.id == row_id).first()
        if not row:
            return {"success": False, "error": "记录不存在"}
        db.delete(row)
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ---------------- 标签设置：素材标签增删改查 ----------------
@router.get("/api/tags")
async def list_material_tags(tag_type: str = ""):
    db = SessionLocal()
    try:
        q = db.query(MaterialTagDB).order_by(MaterialTagDB.id)
        if tag_type:
            q = q.filter(MaterialTagDB.tag_type == tag_type)
        rows = q.all()
        return {"success": True, "data": [
            {"id": r.id, "name": r.name, "color": r.color, "tag_type": r.tag_type} for r in rows
        ]}
    finally:
        db.close()


@router.post("/api/tags")
async def save_material_tag(body: MaterialTagBody):
    """新增或修改标签。
    - 带 id：按主键精确更新该记录
    - 不带 id：按名称查重，存在则更新、不存在则新增
    """
    name = (body.name or "").strip()
    if not name:
        return {"success": False, "error": "标签名称不能为空"}
    color = (body.color or "").strip() or "#1f6feb"
    db = SessionLocal()
    try:
        if body.id is not None:
            row = db.query(MaterialTagDB).filter(MaterialTagDB.id == body.id).first()
            if row:
                conflict = (db.query(MaterialTagDB)
                            .filter(MaterialTagDB.name == name, MaterialTagDB.id != body.id)
                            .first())
                if conflict:
                    return {"success": False, "error": f"标签「{name}」已存在，请使用其它名称"}
                row.name = name
                row.color = color
                row.tag_type = body.tag_type or "public"
                row.update_time = datetime.now()
                db.commit()
                return {"success": True, "data": {"id": row.id, "name": name, "color": color}}
            # 指定的主键不存在，回落为按名称新增/更新
        row = db.query(MaterialTagDB).filter(MaterialTagDB.name == name).first()
        if row:
            row.color = color
            row.tag_type = body.tag_type or "public"
            row.update_time = datetime.now()
        else:
            row = MaterialTagDB(name=name, color=color, tag_type=body.tag_type or "public")
            db.add(row)
        db.commit()
        return {"success": True, "data": {"id": row.id, "name": name, "color": color}}
    finally:
        db.close()


@router.delete("/api/tags/{row_id}")
async def delete_material_tag(row_id: int):
    db = SessionLocal()
    try:
        row = db.query(MaterialTagDB).filter(MaterialTagDB.id == row_id).first()
        if not row:
            return {"success": False, "error": "记录不存在"}
        db.delete(row)
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ---------------- 素材标记：查询某素材已打的标签 ----------------
@router.get("/api/material_marks")
async def get_material_marks(file_path: str = ""):
    db = SessionLocal()
    try:
        if file_path:
            row = db.query(MaterialMarkDB).filter(MaterialMarkDB.file_path == file_path).first()
            tags = [t for t in (row.tags.split(",") if row and row.tags else []) if t]
            return {"success": True, "data": {"file_path": file_path, "tags": tags}}
        rows = db.query(MaterialMarkDB).order_by(MaterialMarkDB.id).all()
        return {"success": True, "data": [
            {"file_path": r.file_path, "tags": [t for t in r.tags.split(",") if t]} for r in rows
        ]}
    finally:
        db.close()


class MaterialMarkBody(BaseModel):
    file_path: str
    tags: list = []


@router.post("/api/material_marks")
async def save_material_marks_api(body: MaterialMarkBody):
    """保存某素材的个人标签（覆盖式更新）。tags 为空列表则清除该素材所有标签。"""
    try:
        _save_material_marks(body.file_path, body.tags)
        return {"success": True, "data": {"file_path": body.file_path, "tags": body.tags}}
    except Exception as e:
        return {"success": False, "error": str(e)}


class SplitPlansBody(BaseModel):
    change_type: str = "换动作"
    extra: str = ""


@router.post("/api/image_split/generate_plans")
async def generate_split_plans(body: SplitPlansBody):
    """根据变化类型和补充想法，AI 生成 3 套模特动作方案。"""
    import os, httpx
    api_key = os.environ.get("NEWAPI_API_KEY", "")
    base = os.environ.get("NEWAPI_BASE", "http://192.168.110.254/v1")
    if not api_key:
        return {"success": False, "error": "未配置 NEWAPI_API_KEY"}
    ct = body.change_type
    if ct == "换场景":
        field = "场景"
        examples = ["原场景附近更开阔的位置，保留背景材质和色调", "有自然光的咖啡馆窗边", "简洁的白色摄影棚，柔光"]
    elif ct == "动作+场景":
        field = "动作+场景"
        examples = ["模特侧身站立，在原木色桌面旁", "模特坐在窗边，阳光从侧面照入", "模特自然行走，背景是浅色街道"]
    else:
        field = "模特动作"
        examples = ["自然站立，一手插兜，视线略侧前方", "身体侧转约 30 度，双手自然垂放", "正面微笑，双手自然下垂"]
    prompt = f"""你是电商摄影指导。用户要对一张服装商品图进行图片裂变，变化类型：{ct}。
补充想法：{body.extra or '无'}

请生成 3 套不同的{field}方案，每套一句话描述（20-40字），具体、可执行、适合电商商品图。
只返回 JSON 数组，3 个字符串，不要其他文字。例如：
{examples}"""
    import json, re
    last_err = ""
    text = ""
    for model in ["deepseek-v4-flash", "deepseek-v3.2-thinking", "gemini-3.8-flash", "qwen3.8-flash", "gpt-5.4-mini"]:
        try:
            resp = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是电商模特摄影指导，只返回 JSON 数组。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.7,
                },
                timeout=60,
                trust_env=False,
            )
            rj = resp.json()
            if resp.status_code == 200 and "choices" in rj:
                text = rj["choices"][0]["message"]["content"].strip()
                break
            last_err = f"{model}: {rj.get('error') or rj}"
        except Exception as e:
            last_err = f"{model}: {e}"
    if not text:
        return {"success": False, "error": f"所有模型均失败: {last_err}"}
    import json, re
    m = re.search(r'\[.*\]', text, re.S)
    if m:
        try:
            plans = json.loads(m.group())
        except Exception:
            plans = [l.strip("-·0123456789. ") for l in text.split("\n") if l.strip()][:3]
    else:
        plans = [l.strip("-·0123456789. ") for l in text.split("\n") if l.strip()][:3]
    if len(plans) < 3:
        plans = (plans + ["自然站立，正面展示", "侧身 45 度，展示侧面", "双手自然下垂，微笑"])[:3]
    return {"success": True, "plans": plans}

class GenImageBody(BaseModel):
    source_path: str = ""
    action: str = ""
    change_type: str = "换动作"


@router.post("/api/image_split/generate_image")
async def generate_split_image(body: GenImageBody):
    """根据原图和动作描述，调用 AI 生成图片裂变结果。"""
    import os, httpx, base64, time
    api_key = os.environ.get("NEWAPI_API_KEY", "")
    base = os.environ.get("NEWAPI_BASE", "http://192.168.110.254/v1")
    if not api_key:
        return {"success": False, "error": "未配置 NEWAPI_API_KEY"}
    # 读取本地原图，转 base64
    src_file = body.source_path
    if not src_file:
        return {"success": False, "error": "缺少原图路径"}
    # media_storage 目录
    media_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "media_storage")
    full = os.path.join(media_dir, src_file.replace("/", os.sep))
    if not os.path.exists(full):
        return {"success": False, "error": f"原图不存在: {full}"}
    try:
        with open(full, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(full)[1].lower().lstrip(".") or "png"
        data_url = f"data:image/{ext};base64,{img_b64}"
    except Exception as e:
        return {"success": False, "error": f"读原图失败: {e}"}

    prompt = f"电商服装商品图裂变。保持商品本身不变，将模特动作改为：{body.action}。要求：{body.change_type}，竖版9:16，干净背景，自然光，真实感。"
    # 尝试多个模型
    last_err = "unknown"
    for model in ["gpt-image-2", "mai-image-2.5", "qwen-image-3.0"]:
        try:
            resp = httpx.post(
                f"{base}/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "prompt": prompt,
                    "n": 1,
                    "size": "1024x1536",
                    "image": data_url,
                },
                timeout=120,
                trust_env=False,
            )
            rj = resp.json()
            if resp.status_code == 200 and rj.get("data"):
                img_url = rj["data"][0].get("url") or rj["data"][0].get("b64_json")
                if img_url and img_url.startswith("data:"):
                    # 保存到本地
                    import re
                    m = re.match(r'data:image/\w+;base64,(.+)', img_url)
                    if m:
                        out_dir = os.path.join(media_dir, "splits")
                        os.makedirs(out_dir, exist_ok=True)
                        fname = f"split_{int(time.time()*1000)}_{os.path.basename(full)}"
                        with open(os.path.join(out_dir, fname), "wb") as fout:
                            fout.write(base64.b64decode(m.group(1)))
                        return {"success": True, "url": f"/api/uploaded_media/splits/{fname}"}
                elif img_url:
                    return {"success": True, "url": img_url}
        except Exception as e:
            last_err = str(e)
    return {"success": False, "error": f"所有模型均失败：{last_err}"}

@router.get("/api/skus")
def list_skus():
    db = SessionLocal()
    try:
        rows = db.query(SkuDB).order_by(SkuDB.id.desc()).all()
        return [{"id": r.id, "series": r.series, "sku_no": r.sku_no, "name": r.name, "brand": r.brand, "category": r.category} for r in rows]
    finally:
        db.close()


class SkuBody(BaseModel):
    series: str = ""
    sku_no: str = ""
    name: str = ""
    brand: str = ""
    category: str = ""


@router.post("/api/skus")
def create_sku(body: SkuBody):
    db = SessionLocal()
    try:
        s = SkuDB(series=body.series, sku_no=body.sku_no, name=body.name, brand=body.brand, category=body.category)
        db.add(s)
        db.commit()
        db.refresh(s)
        return {"success": True, "id": s.id}
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        db.close()

@router.get("/api/launch_records")
async def get_launch_records(advertiser_id: str = "", limit: int = 100):
    """查询 PostgreSQL 投放记录表（倒序）。PG 不可用时返回空列表。"""
    try:
        from ad.pg_db import query_launch_records
        rows = query_launch_records(limit=min(int(limit), 500), advertiser_id=advertiser_id or "")
        return {"success": True, "total": len(rows), "data": rows}
    except Exception as e:
        return {"success": False, "error": str(e), "data": []}




# ============ 同步素材状态 ============
@router.post("/api/sync_material_status")
async def sync_material_status():
    """同步素材投放状态：从千川查询最新审核状态，更新本地数据库"""
    db = SessionLocal()
    try:
        # 查询所有有 material_id 的投放记录
        rows = db.query(MaterialLaunchDB).filter(
            MaterialLaunchDB.material_id != "",
            MaterialLaunchDB.mode == "real"
        ).all()
        
        if not rows:
            return {"success": True, "msg": "没有需要同步的素材", "updated": 0}
        
        updated = 0
        errors = []
        
        # 按 advertiser_id 分组，避免重复创建服务
        ad_services = {}
        
        for row in rows:
            try:
                # 获取或创建广告服务
                aid = row.advertiser_id
                if aid not in ad_services:
                    ad_services[aid] = DouYinAdService(advertiser_id=aid)
                ad_service = ad_services[aid]
                
                # 查询素材库信息
                material_id = row.material_id
                mtype = "视频" if row.detail and "video" in row.detail.lower() else "图片"
                
                # 调用千川接口查询素材状态
                import requests, json
                path = "/open_api/v1.0/qianchuan/image/get/" if mtype == "图片" else "/open_api/v1.0/qianchuan/video/get/"
                
                # 直接用计划详情接口判断素材状态
                try:
                    detail = ad_service.get_overall_plan_detail(row.plan_id)
                    creatives = detail.get("multi_product_creative_list", []) or []
                    
                    # 收集计划详情里的所有图片ID和视频ID，精确匹配本地 material_id
                    found = False
                    new_material_id = material_id
                    all_img_ids = []
                    all_vid_ids = []
                    for c in creatives:
                        imgs = c.get("image_material", []) or []
                        vids = c.get("video_material", []) or []
                        for im in imgs:
                            for iid in (im.get("image_ids", []) or []):
                                all_img_ids.append(str(iid))
                                if str(iid) == material_id:
                                    found = True
                        for v in vids:
                            vid = str(v.get("video_id", "") or "")
                            all_vid_ids.append(vid)
                            if vid == material_id:
                                found = True
                    
                    # 图片素材：追加时图片会先裁剪成商品卡方图再上传，
                    # 计划详情里的 image_ids 是裁剪后新图的 tos 路径，与本地 material_id 不一致。
                    # 只要该计划详情存在图片素材，即视为该图片已投放，并回写计划里的实际 image_ids。
                    if not found and mtype == "图片" and all_img_ids:
                        found = True
                        new_material_id = all_img_ids[0]
                    
                    # 如果在计划里找到了素材，说明已经投放成功，审核通过
                    if found:
                        rj = {"code": 0, "data": {"list": [{"audit_status": "AUDIT_STATUS_APPROVED"}]},
                              "_new_material_id": new_material_id}
                    else:
                        rj = {"code": -1, "message": "在计划里找不到这个素材"}
                except Exception as e:
                    rj = {"code": -1, "message": str(e)}
                
                if rj.get("code") == 0:
                    lst = rj.get("data", {}).get("list", []) or []
                    if lst:
                        item = lst[0]
                        audit_status = item.get("audit_status", "")
                        reject_reason = item.get("reject_reason", "") or item.get("audit_reason", "") or ""
                        
                        # 映射千川审核状态到业务状态
                        new_biz_status = ""
                        if audit_status == "AUDIT_STATUS_APPROVED":
                            new_biz_status = "已投放"
                        elif audit_status == "AUDIT_STATUS_REJECTED":
                            new_biz_status = "审核驳回"
                        elif audit_status == "AUDIT_STATUS_PENDING":
                            new_biz_status = "待审核"
                        else:
                            new_biz_status = row.biz_status  # 保持原状态
                        
                        # 更新数据库（状态、驳回原因或素材ID变化了就更新）
                        changed = False
                        if new_biz_status and new_biz_status != row.biz_status:
                            row.biz_status = new_biz_status
                            changed = True
                        if reject_reason and reject_reason != row.reject_reason:
                            row.reject_reason = reject_reason
                            changed = True
                        new_mid = rj.get("_new_material_id", "")
                        if new_mid and new_mid != row.material_id:
                            row.material_id = new_mid
                            changed = True
                        if changed:
                            row.detail = f"同步更新：{audit_status}" + (f"，原因：{reject_reason}" if reject_reason else "")
                            updated += 1
                else:
                    errors.append(f"素材 {material_id} 查询失败: {rj.get('message', '')}")
                    
            except Exception as e:
                errors.append(f"素材 {row.material_id} 同步异常: {str(e)}")
        
        db.commit()
        
        return {
            "success": True,
            "msg": f"同步完成，更新了 {updated} 条记录",
            "updated": updated,
            "total": len(rows),
            "errors": errors[:10]  # 只返回前10个错误
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()

@router.get("/api/material_launch_status")
async def get_material_launch_status():
    """返回全部素材的投放状态汇总：{file_path: {status, mode, biz_status, count, time, plan_name, product_id, detail}}
    status 取该素材最近一次投放结果（success/fail），mode 区分真实/测试；
    biz_status 业务状态：待审核/审核驳回/通过-待投放/直播间已投放/商城已投放/已投放商品+直播间/放弃测试。"""
    from collections import defaultdict
    db = SessionLocal()
    try:
        rows = db.query(MaterialLaunchDB).order_by(MaterialLaunchDB.id.desc()).all()
        latest = {}
        counts = defaultdict(int)
        for r in rows:
            counts[r.file_path] += 1
            if r.file_path not in latest:
                latest[r.file_path] = r
        out = {}
        for path, r in latest.items():
            biz = getattr(r, "biz_status", "") or ""
            if not biz:
                # 兼容旧记录：按投放结果推导默认业务状态
                if r.status == "fail":
                    biz = "审核驳回" if r.mode == "real" else "放弃测试"
                elif r.mode == "test":
                    biz = "通过-待投放"
                else:
                    biz = "待审核"
            out[path] = {
                "status": r.status,
                "mode": r.mode,
                "biz_status": biz,
                "count": counts[path],
                "time": r.create_time.strftime("%m-%d %H:%M") if r.create_time else "",
                "plan_id": r.plan_id,
                "plan_name": r.plan_name,
                "product_id": r.product_id,
                "detail": r.detail,
            }
        return {"success": True, "data": out}
    finally:
        db.close()


class MaterialBizStatusBody(BaseModel):
    file_path: str
    biz_status: str = ""


@router.post("/api/material/set_biz_status")
async def set_material_biz_status(body: MaterialBizStatusBody):
    """手动设置素材业务投放状态（审核驳回/放弃测试等需人工判断的状态）。"""
    from ad.db import MATERIAL_BIZ_STATUSES
    fp = (body.file_path or "").strip()
    biz = (body.biz_status or "").strip()
    if not fp:
        return {"success": False, "error": "缺少素材文件路径"}
    if biz and biz not in MATERIAL_BIZ_STATUSES:
        return {"success": False, "error": f"无效状态：{biz}"}
    db = SessionLocal()
    try:
        if biz:
            db.add(MaterialLaunchDB(
                file_path=fp, status="success", mode="manual",
                biz_status=biz, detail="手动标注状态",
            ))
        else:
            # 清空：删除该素材最近一条手动标注记录
            rows = db.query(MaterialLaunchDB).filter(
                MaterialLaunchDB.file_path == fp,
                MaterialLaunchDB.mode == "manual",
            ).all()
            for r in rows:
                db.delete(r)
        db.commit()
        return {"success": True, "file_path": fp, "biz_status": biz}
    finally:
        db.close()


# ---------------- 直播投放效果（千川已授权权限：全域投放数据 + 今日直播数据） ----------------
@router.get("/api/live/effect")
async def get_live_effect(advertiser_id: str = "", refresh: bool = False):
    """直播投放效果汇总：
    - anchors: 直播间画面投放数据（按主播：展示/观看/CVR/消耗/ROI/GMV/成交）
    - materials: 直播视频素材数据（素材粒度）
    - boards: 直播大屏（流量来源观看/GMV + 商品列表GMV/成交/ROI/成本）
    仅用千川已授权权限，无需「电商直播数据」权限。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_live_effect_report(force=bool(refresh))
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ---------------- 直播整体数据（千川权限：获取今日直播数据 22100400） ----------------
@router.get("/api/live/overview")
async def get_live_overview(advertiser_id: str = "", refresh: bool = False):
    """直播整体数据（report/live/get，全部流量 自然+营销）：
    按抖音号返回 消耗/点击/观看/点击商品/下单/成单/GMV/ROI/粉丝/互动等。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_live_overview(force=bool(refresh))
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------- 直播间商品列表（千川权限：获取今日直播数据 22100400） ----------------
@router.get("/api/live/room_products")
async def get_live_room_products(advertiser_id: str = "", refresh: bool = False):
    """直播间商品列表（today_live/room/product_list/get/）：
    先从直播大屏拿 room_id，再逐个直播间拉商品（名称/价格/销量/支付/退款/点击/曝光）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        data = svc.get_live_room_products(force=bool(refresh))
        return {"success": True, **data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------- 素材分类体系：增删改查 ----------------
class CategoryBody(BaseModel):
    name: str
    level: int = 1            # 1=主类目 2=一级 3=二级
    parent_id: int = 0         # 父分类ID，0=主类目
    id: Optional[int] = None   # 编辑时传主键


@router.get("/api/categories")
async def list_categories():
    """返回全部分类（平铺，含层级与父级ID），前端按 level/parent_id 渲染树。"""
    db = SessionLocal()
    try:
        rows = db.query(MaterialCategoryDB).order_by(MaterialCategoryDB.level, MaterialCategoryDB.sort, MaterialCategoryDB.id).all()
        return {"success": True, "data": [
            {"id": r.id, "name": r.name, "level": r.level, "parent_id": r.parent_id} for r in rows
        ]}
    finally:
        db.close()


@router.post("/api/categories")
async def save_category(body: CategoryBody):
    name = (body.name or "").strip()
    if not name:
        return {"success": False, "error": "分类名称不能为空"}
    if body.level not in (1, 2, 3):
        return {"success": False, "error": "分类层级必须是 1/2/3"}
    # 二级分类必须选一级父类；一级分类必须选主类目父类
    parent_id = body.parent_id or 0
    if body.level >= 2 and parent_id == 0:
        return {"success": False, "error": f"请选择上级分类"}
    db = SessionLocal()
    try:
        # 同级下名称查重
        q = db.query(MaterialCategoryDB).filter(
            MaterialCategoryDB.level == body.level,
            MaterialCategoryDB.parent_id == parent_id,
            MaterialCategoryDB.name == name,
        )
        if body.id is not None:
            q = q.filter(MaterialCategoryDB.id != body.id)
        if q.first():
            return {"success": False, "error": f"同级下「{name}」已存在"}
        if body.id is not None:
            row = db.query(MaterialCategoryDB).filter(MaterialCategoryDB.id == body.id).first()
            if row:
                row.name = name
                row.level = body.level
                row.parent_id = parent_id
                row.update_time = datetime.now()
                db.commit()
                return {"success": True, "data": {"id": row.id, "name": name, "level": row.level, "parent_id": row.parent_id}}
        row = MaterialCategoryDB(name=name, level=body.level, parent_id=parent_id)
        db.add(row)
        db.commit()
        return {"success": True, "data": {"id": row.id, "name": name, "level": row.level, "parent_id": row.parent_id}}
    finally:
        db.close()


@router.delete("/api/categories/{row_id}")
async def delete_category(row_id: int):
    db = SessionLocal()
    try:
        row = db.query(MaterialCategoryDB).filter(MaterialCategoryDB.id == row_id).first()
        if not row:
            return {"success": False, "error": "分类不存在"}
        # 有子分类则禁止删除
        child = db.query(MaterialCategoryDB).filter(MaterialCategoryDB.parent_id == row_id).first()
        if child:
            return {"success": False, "error": "该分类下还有子分类，请先删除子分类"}
        db.delete(row)
        db.commit()
        return {"success": True}
    finally:
        db.close()




# ---------------- 素材投放状态标签：增删改查 ----------------
DEFAULT_BIZ_STATUSES = [
    ("审核中", "审核中", "#F5B041", 1),
    ("待投放", "待投放", "#A8E05F", 2),
    ("已投放", "已投放", "#5DCEC4", 3),
    ("已暂停", "已暂停", "#B0B5BD", 4),
]

def _ensure_default_biz_statuses(db):
    """首次启动时插入默认状态标签"""
    if db.query(MaterialBizStatusDB).count() > 0:
        return
    for name, code, color, sort in DEFAULT_BIZ_STATUSES:
        db.add(MaterialBizStatusDB(name=name, code=code, color=color, sort=sort))
    db.commit()


@router.get("/api/material_biz_statuses")
async def list_material_biz_statuses():
    db = SessionLocal()
    try:
        _ensure_default_biz_statuses(db)
        rows = db.query(MaterialBizStatusDB).order_by(MaterialBizStatusDB.sort, MaterialBizStatusDB.id).all()
        return {"success": True, "data": [
            {"id": r.id, "name": r.name, "code": r.code, "color": r.color, "sort": r.sort} for r in rows
        ]}
    finally:
        db.close()


class BizStatusBody(BaseModel):
    name: str
    color: str = "#CCFF00"
    id: Optional[int] = None


@router.post("/api/material_biz_statuses")
async def save_material_biz_status(body: BizStatusBody):
    name = (body.name or "").strip()
    if not name:
        return {"success": False, "error": "状态名称不能为空"}
    color = (body.color or "").strip() or "#CCFF00"
    db = SessionLocal()
    try:
        if body.id is not None:
            row = db.query(MaterialBizStatusDB).filter(MaterialBizStatusDB.id == body.id).first()
            if row:
                row.name = name
                row.color = color
                row.update_time = datetime.now()
                db.commit()
                return {"success": True, "data": {"id": row.id, "name": row.name, "code": row.code, "color": row.color}}
        # 新增：code 用 name（用户可改显示名但 code 不变）
        row = MaterialBizStatusDB(name=name, code=name, color=color)
        db.add(row)
        db.commit()
        return {"success": True, "data": {"id": row.id, "name": row.name, "code": row.code, "color": row.color}}
    finally:
        db.close()


@router.delete("/api/material_biz_statuses/{row_id}")
async def delete_material_biz_status(row_id: int):
    db = SessionLocal()
    try:
        row = db.query(MaterialBizStatusDB).filter(MaterialBizStatusDB.id == row_id).first()
        if not row:
            return {"success": False, "error": "状态不存在"}
        db.delete(row)
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ============ 图片裂变：保存结果 ============
class ImageSplitSaveBody(BaseModel):
    source_path: str = ""
    action: str = ""
    change_type: str = "换动作"
    result_url: str = ""
    sku_id: int = 0
    mode: str = "入库"  # 入库 / 入投放

@router.post("/api/image_split/save")
def save_image_split(body: ImageSplitSaveBody):
    db = SessionLocal()
    try:
        rec = MaterialLaunchDB(
            file_path=body.result_url or body.source_path,
            status="success",
            mode="test",
            biz_status="待投放" if body.mode == "入投放" else "已入库",
            plan_id="",
            plan_name="",
            product_id=str(body.sku_id),
            detail=f"裂变方式:{body.change_type}; 动作:{body.action}; 模式:{body.mode}",
        )
        db.add(rec)
        db.commit()
        return {"success": True, "id": rec.id}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()


# ============ 素材状态统计 ============
@router.get("/api/material_stats")
@router.get("/api/material_stats")
@router.get("/api/material_stats")
def material_stats():
    db = SessionLocal()
    try:
        rows = db.query(MaterialLaunchDB).all()
        stats = {"全部素材": 0, "审核中": 0, "待投放": 0, "已投放": 0, "已暂停": 0, "已结束": 0, "未提交": 0}
        
        # 按文件名去重：投放成功记录优先（失败重试不覆盖成功记录），同状态取最新
        db_by_name = {}
        for r in rows:
            fname = os.path.basename((r.file_path or "").replace("\\", "/"))
            if fname not in db_by_name:
                db_by_name[fname] = r
                continue
            cur = db_by_name[fname]
            # 投放成功记录优先（失败重试不覆盖）
            if r.status == "success" and cur.status != "success":
                db_by_name[fname] = r
                continue
            if cur.status == "success" and r.status != "success":
                continue
            # 同状态：已回填素材ID（material_id）的记录优先
            if r.material_id and not cur.material_id:
                db_by_name[fname] = r
                continue
            if cur.material_id and not r.material_id:
                continue
            # 其余取最新
            if r.create_time > cur.create_time:
                db_by_name[fname] = r
        
        # 统计上传目录中的所有素材文件（和 db_materials 一致）
        upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "media_storage")
        if not os.path.exists(upload_dir):
            upload_dir = r"C:\Users\33082\PycharmProjects\wellflow-saas-backend\ad\media_storage"
        
        all_files = []
        if os.path.exists(upload_dir):
            for f in os.listdir(upload_dir):
                fpath = os.path.join(upload_dir, f)
                if os.path.isfile(fpath):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp4", ".mov", ".avi", ".mkv", ".webm"]:
                        all_files.append((fpath, f))
        
        # 统计每个文件的状态
        for fpath, fname in all_files:
            r = db_by_name.get(fname)
            biz = ""
            if r:
                biz = r.biz_status or ""
                if r.material_id:
                    biz = "已投放"  # 素材已挂到计划即视为已投放
                elif r.plan_id and not biz:
                    biz = "待投放"
            
            if biz == "待审核" or biz == "审核中":
                stats["审核中"] += 1
            elif biz == "通过-待投放" or biz == "待投放":
                stats["待投放"] += 1
            elif biz in ["直播间已投放", "商城已投放", "已投放商品+直播间", "已投放"]:
                stats["已投放"] += 1
            elif biz == "审核驳回" or biz == "已暂停":
                stats["已暂停"] += 1
            elif biz == "放弃测试" or biz == "已结束":
                stats["已结束"] += 1
            else:
                stats["未提交"] += 1
        
        stats["全部素材"] = len(all_files)
        
        return stats
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()
@router.get("/api/db_materials")
def db_materials(shop: str = ""):
    db = SessionLocal()
    try:
        # 店铺过滤：店铺名 → 广告主ID（ads_shops），再结合投放记录表 material_launch 过滤
        shop_ad_ids = set()
        if shop:
            rows = db.execute(text("SELECT advertiser_id FROM ads_shops WHERE shop_name = :s"), {"s": shop}).fetchall()
            shop_ad_ids = {str(r[0]) for r in rows}
            if not shop_ad_ids:
                return {"success": True, "data": [], "count": 0}
        
        rows = db.query(MaterialLaunchDB).order_by(MaterialLaunchDB.create_time.desc()).all()
        if shop_ad_ids:
            # 只保留该店铺投放记录关联的素材；选店铺时只显示有投放动作（plan_id 有）的素材
            rows = [r for r in rows if str(r.advertiser_id or "") in shop_ad_ids and r.plan_id]
        data = []
        # 同时从上传目录找真实文件
        upload_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "media_storage")
        local_files = {}
        if os.path.exists(upload_dir):
            for f in os.listdir(upload_dir):
                local_files[f] = os.path.join(upload_dir, f)
        
        # 按文件名去重，取最新记录（与 material_stats 统计口径一致）
        by_name = {}
        for r in rows:
            fname = os.path.basename((r.file_path or "").replace("\\", "/"))
            if fname not in by_name:
                by_name[fname] = r
                continue
            cur = by_name[fname]
            # 投放成功记录优先（失败重试不覆盖）
            if r.status == "success" and cur.status != "success":
                by_name[fname] = r
                continue
            if cur.status == "success" and r.status != "success":
                continue
            # 同状态：已回填素材ID（material_id）的记录优先
            if r.material_id and not cur.material_id:
                by_name[fname] = r
                continue
            if cur.material_id and not r.material_id:
                continue
            # 其余取最新
            if r.create_time > cur.create_time:
                by_name[fname] = r
        
        # 从数据库记录生成素材列表
        for fname, r in by_name.items():
            real_path = r.file_path
            if not os.path.exists(real_path) and fname in local_files:
                real_path = local_files[fname]
            if not os.path.exists(real_path):
                continue
            ext = os.path.splitext(fname)[1].lower()
            ftype = "video" if ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"] else "image"
            # 与 material_stats 相同的状态推算：空状态 + material_id → 已投放；空状态 + plan_id → 待投放
            biz = r.biz_status or ""
            if r.material_id:
                biz = "已投放"  # 素材已挂到计划即视为已投放
            elif r.plan_id and not biz:
                biz = "待投放"
            data.append({
                "id": r.id,
                "name": fname,
                "path": real_path,
                "type": ftype,
                "size": os.path.getsize(real_path),
                "mtime": os.path.getmtime(real_path),
                "biz_status": biz or "未提交",
                "plan_id": r.plan_id or "",
                "plan_name": r.plan_name or "",
                "detail": r.detail or "",
                "reject_reason": getattr(r, 'reject_reason', '') or '',
                "material_id": r.material_id or "",
                "advertiser_id": r.advertiser_id or "",
            })
        
        # 补充上传目录中未在数据库中的素材（仅未选店铺时补充，选店铺时只显示该店铺投放过的素材）
        existing_paths = {d["path"] for d in data}
        if not shop_ad_ids:
          for fname, fpath in local_files.items():
            if fpath in existing_paths:
                continue
            ext = os.path.splitext(fname)[1].lower()
            ftype = "video" if ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"] else "image"
            st = os.stat(fpath)
            data.append({
                "id": hash(fpath) % 1000000,
                "name": fname,
                "path": fpath,
                "type": ftype,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "biz_status": "未提交",
                "plan_name": "",
                "detail": "",
            })
        
        # 按修改时间倒序
        data.sort(key=lambda x: x["mtime"], reverse=True)
        
        return {"success": True, "data": data, "count": len(data)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()


# ============ 根据完整路径返回文件 ============
@router.get("/api/file_by_path")
def file_by_path(path: str):
    if not os.path.exists(path) or not os.path.isfile(path):
        return {"error": "文件不存在"}
    return FileResponse(path)

