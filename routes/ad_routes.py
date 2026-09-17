import os
from datetime import datetime
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from models import AdLaunchRequest, AdLaunchResult
from douyin_api import DouYinAdService
from file_service import delete_media_file
from token_manager import get_token_mgr
from config import DOUYIN_CONFIG
from db import SessionLocal, AdvertiserDB, MaterialTagDB, MaterialMarkDB, MaterialLaunchDB

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
                        product_id: str = "", detail: str = ""):
    """记录一次素材投放历史，用于素材库展示投放状态"""
    if not file_path:
        return
    db = SessionLocal()
    try:
        db.add(MaterialLaunchDB(
            file_path=file_path, status=status, mode=mode,
            plan_id=plan_id or "", plan_name=plan_name or "",
            product_id=product_id or "", detail=(detail or "")[:300],
        ))
        db.commit()
    finally:
        db.close()


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
                                detail="测试模式模拟投放")
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
            new_ad_id = ad_service.add_video_to_plan(req.plan_id, f_path)
            result = AdLaunchResult(
                success=True,
                local_file_path=f_path,
                advertiser_id=advertiser_id,
                ad_plan_id=new_ad_id,
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
                                plan_id=req.plan_id, product_id=",".join(req.product_ids or []),
                                detail="已追加到投放计划")
            delete_media_file(req.local_file_path)
        else:
            _save_launch_record(req.local_file_path, "fail", "real",
                                plan_id=req.plan_id or "", product_id=",".join(req.product_ids or []),
                                detail=result.error_msg or "投放失败")
        return result

    elif req.platform in ["jd", "taobao"]:
        return {"success": False, "msg": "京东淘宝待实现，骨架已预留"}


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


@router.get("/api/ad/report")
async def get_report(advertiser_id: str, creative_id: str):
    ad_service = DouYinAdService(advertiser_id=advertiser_id)
    report_list = ad_service.get_ad_report_data(creative_id=creative_id)
    return {"data": [item.model_dump() for item in report_list]}


@router.get("/api/products")
async def list_products(advertiser_id: str = "", refresh: bool = False):
    """返回可投商品，并标注每个商品当前已挂的素材数/在投计划。可指定 advertiser_id 查询对应账户的商品。
    refresh=true 时强制绕过缓存，从千川重新拉取（约10-30秒）。"""
    try:
        aid = advertiser_id or DOUYIN_CONFIG.get("DEFAULT_ADVERTISER_ID")
        svc = DouYinAdService(advertiser_id=aid)
        # 商品列表与素材统计并行拉取（避免串行累加耗时）
        def _load_products():
            return svc.get_available_products(force=refresh)

        def _load_stats():
            return svc.get_product_material_stats(force=refresh)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_load_products)
            f2 = ex.submit(_load_stats)
            products = f1.result()
            stats = f2.result()
        for p in products:
            st = stats.get(p["id"], {})
            p["material_count"] = st.get("material_count", 0)
            p["plan_id"] = st.get("plan_id", "")
            p["plan_status"] = st.get("status", "")
        return {"success": True, "data": products}
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
async def list_material_tags():
    db = SessionLocal()
    try:
        rows = db.query(MaterialTagDB).order_by(MaterialTagDB.id).all()
        return {"success": True, "data": [
            {"id": r.id, "name": r.name, "color": r.color} for r in rows
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
                row.update_time = datetime.now()
                db.commit()
                return {"success": True, "data": {"id": row.id, "name": name, "color": color}}
            # 指定的主键不存在，回落为按名称新增/更新
        row = db.query(MaterialTagDB).filter(MaterialTagDB.name == name).first()
        if row:
            row.color = color
            row.update_time = datetime.now()
        else:
            row = MaterialTagDB(name=name, color=color)
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


@router.get("/api/material_launch_status")
async def get_material_launch_status():
    """返回全部素材的投放状态汇总：{file_path: {status, mode, count, time, plan_name, product_id, detail}}
    status 取该素材最近一次投放结果（success/fail），mode 区分真实/测试。"""
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
            out[path] = {
                "status": r.status,
                "mode": r.mode,
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
