import datetime
import hashlib
import json
import os
import subprocess
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
import requests
from models import AdLaunchResult, AdReportItem

from token_manager import get_token_mgr
from config import DOUYIN_CONFIG, BASE_DIR


# 千川素材报表（qianchuan/report/uni_promotion/data/get）指标字段 → 中文名。
# 未覆盖到的字段在前端直接显示原字段名，不影响展示。
_METRIC_CN = {
    "stat_cost_for_roi2": "消耗(ROI2口径)",
    "product_show_count_for_roi2": "整体展示次数",
    "product_click_count_for_roi2": "商品点击次数",
    "product_cvr_rate_for_roi2": "点击率",
    "product_convert_rate_for_roi2": "转化率",
    "total_pay_order_count_for_roi2": "成交订单数",
    "total_pay_order_gmv_for_roi2": "成交金额",
    "total_prepay_and_pay_order_roi2": "支付ROI",
    "stat_cost_for_roi1": "消耗(ROI1口径)",
    "product_show_count_for_roi1": "整体展示次数(ROI1)",
    "product_click_count_for_roi1": "商品点击次数(ROI1)",
    "product_cvr_rate_for_roi1": "点击率(ROI1)",
    "product_convert_rate_for_roi1": "转化率(ROI1)",
    "total_pay_order_count_for_roi1": "成交订单数(ROI1)",
    "total_pay_order_gmv_for_roi1": "成交金额(ROI1)",
    "total_prepay_and_pay_order_roi1": "支付ROI(ROI1)",
    "product_show_count": "整体展示次数",
    "product_click_count": "商品点击次数",
    "stat_cost": "消耗",
    "pay_order_count": "成交订单数",
    "pay_order_gmv": "成交金额",
    "prepay_and_pay_order_roi": "支付ROI",
    "show_cnt": "展示次数",
    "click_cnt": "点击次数",
    "convert_cnt": "转化数",
    "convert_rate": "转化率",
    "avg_click_cost": "点击均价",
    "avg_show_cost": "千次展示均价",
    "deep_convert_cnt": "深度转化数",
    "deep_convert_rate": "深度转化率",
    "first_order_count": "首单数",
    "first_order_gmv": "首单金额",
    "first_order_pay_roi": "首单支付ROI",
    "dy_share_cnt": "转发数",
    "dy_comment_cnt": "评论数",
    "dy_like_cnt": "点赞数",
    "dy_follow_cnt": "关注数",
    "qianchuan_first_order_roi30": "店铺首单新客30天支付ROI",
    "ad_live_order_settle_roi_7d": "直接结算ROI(7天)",
    "ad_all_order_settle_roi_7d": "全部结算ROI(7天)",
    "ad_all_order_settle_roi_14d": "全部结算ROI(14天)",
    "create_order_roi": "直接下单ROI",
    # —— 获取全域投放计划下素材接口（uni_promotion/ad/material/get）完整指标 ——
    "total_cost_per_pay_order_for_roi2": "整体成交订单成本",
    "total_pay_order_coupon_amount_for_roi2": "成交智能优惠券金额",
    "total_unfinished_estimate_order_gmv_for_roi2": "未完结预售订单预估金额",
    "total_ecom_platform_subsidy_amount_for_roi2": "电商平台补贴金额",
    "total_pay_order_gmv_include_coupon_for_roi2": "整体成交金额(含优惠券)",
    "total_cost_per_pay_order_settle_for_roi2_1h": "净成交订单成本",
    "total_order_settle_count_for_roi2_1h": "净成交订单数",
    "total_order_settle_amount_for_roi2_1h": "净成交金额",
    "total_prepay_and_pay_settle_roi2_1h": "净成交ROI",
    "total_order_real_settle_amount_for_roi2_1h": "用户实际支付净成交金额",
    "no_refund_ecom_coupon_amount_for_roi2": "智能优惠券未退款金额",
    "no_refund_ecom_platform_subsidy_amount_for_roi2": "电商平台补贴未退款金额",
    "total_order_settle_count_rate_for_roi2_1h": "净成交订单结算率",
    "total_order_settle_amount_rate_for_roi2_1h": "净成交金额结算率",
    "total_refund_order_count_for_roi2_1h": "1小时内退款订单数",
    "total_refund_order_gmv_for_roi2_1h_all": "1小时内退款金额",
    "total_refund_order_gmv_for_roi2_1h_rate": "1小时内退款率",
    "live_show_count_for_roi2_v2": "直播全域整体展示次数",
    "live_watch_count_for_roi2_v2": "直播全域整体点击次数",
    "live_cvr_rate_for_roi2_v2": "直播全域整体点击率",
    "live_convert_rate_for_roi2_v2": "直播全域整体转化率",
    "live_show_count_exclude_video_for_roi2": "直播全域展示次数(直播间)",
    "live_watch_count_exclude_video_for_roi2": "直播全域点击次数(直播间)",
    "live_cvr_rate_exclude_video_for_roi2": "直播全域点击率(直播间)",
    "live_convert_rate_exclude_video_for_roi2": "直播全域转化率(直播间)",
}

# 获取全域投放计划下素材接口（uni_promotion/ad/material/get）的完整指标字段集。
# 请求时按此列表取 fields，返回的 stats_info 全部按此展示。
_MATERIAL_STATS_FIELDS = [
    "product_show_count_for_roi2", "product_click_count_for_roi2",
    "product_cvr_rate_for_roi2", "product_convert_rate_for_roi2",
    "stat_cost_for_roi2", "total_prepay_and_pay_order_roi2",
    "total_pay_order_gmv_for_roi2", "total_pay_order_count_for_roi2",
    "total_cost_per_pay_order_for_roi2", "total_pay_order_coupon_amount_for_roi2",
    "total_unfinished_estimate_order_gmv_for_roi2", "total_ecom_platform_subsidy_amount_for_roi2",
    "total_pay_order_gmv_include_coupon_for_roi2",
    "total_cost_per_pay_order_settle_for_roi2_1h", "total_order_settle_count_for_roi2_1h",
    "total_order_settle_amount_for_roi2_1h", "total_prepay_and_pay_settle_roi2_1h",
    "total_order_real_settle_amount_for_roi2_1h", "no_refund_ecom_coupon_amount_for_roi2",
    "no_refund_ecom_platform_subsidy_amount_for_roi2", "total_order_settle_count_rate_for_roi2_1h",
    "total_order_settle_amount_rate_for_roi2_1h", "total_refund_order_count_for_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_all", "total_refund_order_gmv_for_roi2_1h_rate",
]


# ============================================================
# TTL 内存缓存：素材报表/详情/预览的千川侧数据短期内不会变，
# 加缓存避免每次打开页面都重新全量拉取（千川接口很慢）。
# ============================================================
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key):
    item = _CACHE.get(key)
    if not item:
        return None
    ts, ttl, value = item
    if _time.time() - ts > ttl:
        return None
    return value


def _cache_set(key, value, ttl):
    with _CACHE_LOCK:
        _CACHE[key] = (_time.time(), ttl, value)


# 千川报表接口并发上限（避免触发平台限流 40100）
_REPORT_SEM = threading.Semaphore(8)


class DouYinAdService:
    """巨量千川（Qianchuan）广告投放服务。

    已从已下线的 v1.0 旧广告 API 迁移到千川链路：
      - 素材上传：/open_api/2/file/video/ad/、/open_api/2/file/image/ad/
      - 计划组创建：/open_api/v1.0/qianchuan/campaign/create/
      - 计划创建（含创意）：/open_api/v1.0/qianchuan/ad/create/
      - 报表：/open_api/v1.0/qianchuan/report/ad/get/
    """

    def __init__(self, advertiser_id: str):
        self.advertiser_id = advertiser_id
        self.base_url = "https://api.oceanengine.com"
        self.access_token = get_token_mgr().get_access_token()
        self.headers = {
            "Access-Token": self.access_token
        }

    # ---------------------------------------------------------------
    # 1. 素材上传（千川 v2 素材库）
    # ---------------------------------------------------------------
    @staticmethod
    def _file_md5(file_path: str) -> str:
        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5.update(chunk)
        return md5.hexdigest()

    @staticmethod
    def _extract_cover_frame(video_path: str) -> str:
        """从视频第1秒处截一帧作为封面，短边放大到至少720（千川竖版封面最小720x1280），返回临时 jpg 路径。"""
        import imageio_ffmpeg
        out = os.path.join(BASE_DIR, "media_storage", "_cover_tmp.jpg")
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        # 短边放到720，保持原宽高比；竖版→720x1280，横版→1280x720
        vf = "scale='if(gt(iw,ih),-2,720)':'if(gt(iw,ih),720,-2)'"
        cmd = [ffmpeg, "-y", "-ss", "1", "-i", video_path, "-frames:v", "1",
               "-vf", vf, "-q:v", "2", out]
        # Windows 中文 locale 下不能 text=True（ffmpeg stderr 含非GBK字节会崩），直接丢弃输出
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return out

    @staticmethod
    def _to_square_image(image_path: str) -> str:
        """把图片中心裁成 1:1 正方形（千川商品卡方图要求），返回临时 jpg 路径。"""
        from PIL import Image
        out = os.path.join(BASE_DIR, "media_storage", "_square_tmp.jpg")
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
            im.save(out, "JPEG", quality=88)
        return out

    def upload_local_media_get_material_id(self, local_file_path: str, file_type: str) -> str:
        """上传本地视频/图片素材，返回 video_id / image_id。

        视频格式：mp4、mpeg、3gp、avi；上传需带文件 MD5（video_signature/image_signature）。
        """
        if file_type == "video":
            url = f"{self.base_url}/open_api/2/file/video/ad/"
            signature = self._file_md5(local_file_path)
            with open(local_file_path, "rb") as f:
                files = {"video_file": f}
                data = {
                    "advertiser_id": self.advertiser_id,
                    "upload_type": "UPLOAD_BY_FILE",
                    "video_signature": signature,
                }
                resp = requests.post(url, headers=self.headers, data=data, files=files, timeout=300)
        elif file_type == "image":
            url = f"{self.base_url}/open_api/2/file/image/ad/"
            signature = self._file_md5(local_file_path)
            with open(local_file_path, "rb") as f:
                files = {"image_file": f}
                data = {
                    "advertiser_id": self.advertiser_id,
                    "upload_type": "UPLOAD_BY_FILE",
                    "image_signature": signature,
                }
                resp = requests.post(url, headers=self.headers, data=data, files=files, timeout=120)
        else:
            raise Exception(f"不支持的素材类型 {file_type}")

        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(f"素材上传失败 code:{resp_json.get('code')}, msg:{resp_json.get('message')}")
        data = resp_json["data"]
        if file_type == "image":
            # 图片上传返回字段为 data.id（tos 路径字符串）
            return data["id"]
        return data["video_id"]

    # ---------------------------------------------------------------
    # 2. 创建全域投放计划（乘方 overall_video/create，含创意，无单独计划组）
    #    商品标准投放已下线，官方要求迁移到全域投放
    # ---------------------------------------------------------------
    def create_overall_plan(self,
                            plan_name: str,
                            product_ids: List[str],
                            video_id: str,
                            aweme_id: str,
                            creative_title: str,
                            video_cover_id: str = "",
                            image_id: str = "",
                            budget: float = 300.0,
                            roi_goal: float = 1.5) -> str:
        """全域投放（uni_aweme/ad/create，商品全域）：一个接口直接创建完整计划（含创意）。

        出价方式为支付ROI目标（净成交ROI控成本），不是传统转化出价。
        传 image_id 时用图片素材（商品卡方图），否则用视频素材。
        """
        url = f"{self.base_url}/open_api/v1.0/qianchuan/uni_aweme/ad/create/"
        # 全域创意标题长度要求 10~110 字符（汉字算2位）
        title = (creative_title or "").strip()
        if len(title) < 6:
            title = "秋冬新品加绒保暖热销中"
        elif len(title) > 55:
            title = title[:55]
        # 素材槽位：图片走 image_material(商品卡方图)，视频走 video_material
        material_slot = {}
        title_material = [{"title": title, "title_type": "CUSTOM"}]
        if image_id:
            material_slot["image_material"] = [
                {"image_mode": "SQUARE", "image_ids": [image_id]}
            ]
            # 商品卡图必须配商品卡标题；且文案不能和普通标题重复（否则报「重复创意标题」）
            # 用真实商品名作商品卡标题，既满足品名要求又和普通标题区分开
            card_title = title
            try:
                for p in self.get_available_products():
                    if str(p["id"]) == str(product_ids[0]):
                        card_title = p["name"] or title
                        break
            except Exception:
                pass
            if card_title == title:
                card_title = (title + " 商品详情")
            if len(card_title) > 55:
                card_title = card_title[:55]
            title_material.append({"title": card_title, "title_type": "COMMODITY_CARD"})
        else:
            material_slot["video_material"] = [
                {
                    "image_mode": DOUYIN_CONFIG.get("QIANCHUAN_IMAGE_MODE", "VIDEO_VERTICAL"),
                    "video_id": video_id,
                    "video_cover_id": video_cover_id,
                }
            ]
        # 全域有号商家必须传投放卡片 creative_card；卡片配图用视频封面或商品方图
        card_image_id = video_cover_id if video_id else image_id
        creative_card = {
            "promotion_card_title": "视频同款商品",
            "promotion_card_selling_points": ["秋冬新款加绒保暖"],
            "promotion_card_image_id": card_image_id,
            "promotion_card_action_button": "专属优惠",
        }
        payload = {
            "advertiser_id": int(self.advertiser_id),
            "name": plan_name,
            "marketing_goal": "VIDEO_PROM_GOODS",   # 商品投放
            "product_ids": [int(p) for p in product_ids],
            "delivery_setting": {
                "smart_bid_type": "SMART_BID_CUSTOM",        # 控成本投放
                "roi2_goal": float(roi_goal),                # 支付ROI目标（如 1.5 = 投产比1.5）
                "qcpx_mode": "QCPX_MODE_OFF",
                "budget": max(float(budget), 300.0),         # 日预算最低 300 元
                "video_schedule_type": "SCHEDULE_FROM_NOW",  # 从今天起长期投放
                "deep_external_action": "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",  # 净成交ROI
            },
            "multi_product_creative_list": [
                dict({
                    "product_id": int(product_ids[0]),
                    "aweme_uid": int(aweme_id),
                    "creative_type": "PROGRAMMATIC_CREATIVE",
                    "title_material": title_material,
                    "creative_card": creative_card,
                }, **material_slot)
            ],
        }
        resp = requests.post(url, headers=self.headers, json=payload, timeout=30)
        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(self._friendly_error("创建全域计划失败", resp_json))
        return str(resp_json["data"]["ad_id"])

    # ---------------------------------------------------------------
    # 4. 数据报表（千川 计划维度报表）
    # ---------------------------------------------------------------
    def get_ad_report_data(self,
                           ad_group_id=None,
                           ad_plan_id=None,
                           creative_id=None,
                           start_date: str = None,
                           end_date: str = None) -> List[AdReportItem]:
        if not start_date:
            start_date = datetime.date.today().strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.date.today().strftime("%Y-%m-%d")

        url = f"{self.base_url}/open_api/v1.0/qianchuan/report/ad/get/"
        payload = {
            "advertiser_id": self.advertiser_id,
            "start_date": start_date,
            "end_date": end_date,
            "dimensions": ["ad_id"],
            "metrics": [
                "stat_cost",
                "show_cnt",
                "click_cnt",
                "convert_cnt",
                "pay_order_count",
                "pay_order_amount",
                "create_order_count",
                "ctr",
                "cpc",
                "cpm",
                "convert_rate",
            ],
            "page": 1,
            "page_size": 100,
        }
        filtering = {}
        if ad_plan_id:
            filtering["ad_id"] = [ad_plan_id]
        if filtering:
            payload["filtering"] = filtering

        resp = requests.post(url, headers=self.headers, json=payload, timeout=30)
        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(self._friendly_error("获取报表失败", resp_json))

        report_items = []
        for row in resp_json.get("data", {}).get("list", []):
            item = AdReportItem(
                creative_id="",
                ad_plan_id=str(row.get("ad_id", "")),
                ad_group_id="",
                show=int(row.get("show_cnt", 0)),
                click=int(row.get("click_cnt", 0)),
                cost=float(row.get("stat_cost", 0)),
                convert=int(row.get("convert_cnt", 0)),
                ctr=float(row.get("ctr", 0)),
                cpc=float(row.get("cpc", 0)),
                cpm=float(row.get("cpm", 0)),
                convert_rate=float(row.get("convert_rate", 0)),
                report_date=start_date,
                extra={
                    "pay_order_count": row.get("pay_order_count", 0),
                    "pay_order_amount": row.get("pay_order_amount", 0),
                    "create_order_count": row.get("create_order_count", 0),
                }
            )
            report_items.append(item)
        return report_items

    # ---------------------------------------------------------------
    # 5. 完整投放流程：上传素材 -> 全域投放建计划（含创意）
    # ---------------------------------------------------------------
    def full_ad_launch_flow(self,
                            local_file_path: str,
                            ad_group_name: str,
                            ad_plan_name: str,
                            creative_name: str,
                            file_type: str,
                            budget: float = 300.0,
                            bid: float = 1.0,
                            aweme_id: Optional[str] = None,
                            product_ids: Optional[List[str]] = None,
                            roi_goal: Optional[float] = None) -> AdLaunchResult:
        try:
            # 广告主 ID 校验
            if not self.advertiser_id:
                return AdLaunchResult(
                    success=False, local_file_path=local_file_path, error_msg="未确定广告主ID"
                )

            # 抖音号 ID：请求参数 > 配置文件
            aweme_id = aweme_id or DOUYIN_CONFIG.get("AWEME_ID") or ""
            if not aweme_id:
                return AdLaunchResult(
                    success=False,
                    local_file_path=local_file_path,
                    advertiser_id=self.advertiser_id,
                    error_msg="缺少投放抖音号 aweme_id，请在 config.py 的 AWEME_ID 或请求参数中填写",
                )

            # 商品 ID 列表：请求参数 > 配置文件
            product_ids = product_ids or DOUYIN_CONFIG.get("PRODUCT_IDS") or []
            if not product_ids:
                return AdLaunchResult(
                    success=False,
                    local_file_path=local_file_path,
                    advertiser_id=self.advertiser_id,
                    error_msg="缺少投放商品 product_ids，请在 config.py 的 PRODUCT_IDS 或请求参数中填写",
                )

            # ROI 目标：请求参数 > 配置文件
            roi_goal = roi_goal if roi_goal else DOUYIN_CONFIG.get("ROI_GOAL", 1.5)

            # 1. 上传素材：视频走 video_id+封面，图片走 image_id（中心裁成正方形）
            cover_id = ""
            image_id = ""
            cover_tmp = None
            if file_type == "image":
                square_tmp = None
                try:
                    square_tmp = self._to_square_image(local_file_path)
                    image_id = self.upload_local_media_get_material_id(square_tmp, "image")
                finally:
                    if square_tmp and os.path.exists(square_tmp):
                        try: os.remove(square_tmp)
                        except OSError: pass
                material_id = image_id
                video_id = ""
            else:
                material_id = self.upload_local_media_get_material_id(local_file_path, "video")
                try:
                    cover_tmp = self._extract_cover_frame(local_file_path)
                    cover_id = self.upload_local_media_get_material_id(cover_tmp, "image")
                except Exception as ce:
                    return AdLaunchResult(
                        success=False,
                        local_file_path=local_file_path,
                        advertiser_id=self.advertiser_id,
                        error_msg=f"封面截取/上传失败：{ce}",
                    )
                finally:
                    if cover_tmp and os.path.exists(cover_tmp):
                        try:
                            os.remove(cover_tmp)
                        except OSError:
                            pass
                video_id = material_id
            # 2. 全域投放：直接创建完整计划（含创意）-> ad_id
            ad_plan_id = self.create_overall_plan(
                plan_name=ad_plan_name,
                product_ids=product_ids,
                video_id=video_id,
                aweme_id=aweme_id,
                creative_title=creative_name,
                video_cover_id=cover_id,
                image_id=image_id,
                budget=budget,
                roi_goal=roi_goal,
            )
            return AdLaunchResult(
                success=True,
                local_file_path=local_file_path,
                advertiser_id=self.advertiser_id,
                material_id=material_id,
                ad_plan_id=ad_plan_id,
                audit_status="PENDING",
            )
        except Exception as e:
            return AdLaunchResult(
                success=False,
                local_file_path=local_file_path,
                advertiser_id=self.advertiser_id,
                error_msg=str(e),
            )

    @staticmethod
    def _friendly_error(prefix: str, resp_json: dict) -> str:
        """把常见千川错误码转成可操作的提示。"""
        code = resp_json.get("code")
        msg = resp_json.get("message", "")
        hint = {
            40002: "千川接口权限不足：请在巨量引擎开放平台「应用详情-开发配置」重新授权，勾选『投放管理-计划管理/计划组管理』『素材管理』等千川权限点，并用新 auth_code 重新授权",
            40136: "抖音号ID(aweme_id)与投放账户不存在生效中的授权关系，请通过『获取千川账户下可投放抖音号』接口核实",
            10002: "请求参数错误，请检查参数拼写与取值范围",
        }.get(code)
        extra = f"（{hint}）" if hint else ""
        return f"{prefix} code:{code}, msg:{msg}{extra}"

    # ===============================================================
    # 可投商品列表 + 每个商品已挂素材数
    # ===============================================================
    def get_available_products(self, force: bool = False) -> list:
        """拉取本账户可投商品（自动翻页，过滤下架/无库存 inventory<=0 的商品），
        返回 [{id, name}]。结果缓存 5 分钟。"""
        cache_key = ("products", str(self.advertiser_id))
        if not force:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached
        out = []
        page = 1
        while True:
            url = f"{self.base_url}/open_api/v1.0/qianchuan/product/available/get/"
            params = {"advertiser_id": int(self.advertiser_id), "page": page, "page_size": 100}
            resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            j = resp.json()
            if j.get("code") != 0:
                raise Exception(self._friendly_error("查询商品列表失败", j))
            data = j.get("data", {})
            for p in data.get("product_list", []):
                # 过滤下架/无库存商品：inventory<=0 的不展示
                if (p.get("inventory") or 0) <= 0:
                    continue
                out.append({"id": str(p.get("id")), "name": p.get("name", "")})
            page_info = data.get("page_info", {}) or {}
            total_page = page_info.get("total_page", 1)
            if page >= total_page:
                break
            page += 1
        _cache_set(cache_key, out, 300)
        return out

    def _fetch_plan_detail_stats(self, ad_id, status):
        """读单个全域计划详情，返回 (ad_id, status, [(product_id, 视频素材数)])；失败返回 (ad_id, status, [])。
        千川详情接口偶发失败/限流，失败时重试 1 次。"""
        for attempt in (1, 2):
            try:
                d = requests.get(f"{self.base_url}/open_api/v1.0/qianchuan/uni_promotion/ad/detail/",
                                 headers=self.headers,
                                 params={"advertiser_id": int(self.advertiser_id), "ad_id": int(ad_id)},
                                 timeout=8).json()
                if d.get("code") != 0:
                    if attempt == 2:
                        return ad_id, status, []
                    continue
                out = []
                for c in (d.get("data", {}) or {}).get("multi_product_creative_list", []) or []:
                    pid = str(c.get("product_id"))
                    n = len([v for v in (c.get("video_material") or []) if v.get("video_id")])
                    out.append((pid, n))
                return ad_id, status, out
            except Exception:
                if attempt == 2:
                    return ad_id, status, []
        return ad_id, status, []

    def get_product_material_stats(self, force: bool = False) -> dict:
        """遍历在投全域计划，统计每个 product_id 已挂多少个视频素材。
        返回 {product_id: {"material_count": n, "plan_id": "..", "status": ".."}}。
        已优化：计划详情并发读取 + 5 分钟 TTL 缓存。"""
        cache_key = ("prod_stats", str(self.advertiser_id))
        if not force:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached
        url = f"{self.base_url}/open_api/v1.0/qianchuan/uni_promotion/list/"
        params = {
            "advertiser_id": int(self.advertiser_id),
            "start_time": (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d 00:00:00"),
            "end_time": datetime.datetime.now().strftime("%Y-%m-%d 23:59:59"),
            "marketing_goal": "VIDEO_PROM_GOODS",
            "filtering": json.dumps({"status": "DELIVERY_OK"}),
            "fields": json.dumps(["stat_cost"]),
            "page": 1,
            "page_size": 100,
        }
        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        j = resp.json()
        result = {}
        if j.get("code") != 0:
            return result  # 查询失败不阻塞商品下拉框
        plan_list = []
        for p in (j.get("data", {}) or {}).get("ad_list", []) or []:
            info = p.get("ad_info", {}) or {}
            ad_id = info.get("id")
            status = info.get("status")
            if ad_id:
                plan_list.append((str(ad_id), status))
        # 并发读各计划详情（信号量限制并发数）
        with ThreadPoolExecutor(max_workers=15) as ex:
            futures = [ex.submit(self._fetch_plan_detail_stats, ad_id, status)
                       for ad_id, status in plan_list]
            for fu in as_completed(futures):
                ad_id, status, items = fu.result()
                for pid, n in items:
                    result[pid] = {"material_count": n, "plan_id": ad_id, "status": status}
        _cache_set(cache_key, result, 600)
        return result

    # 千川全域计划状态 → 中文
    _PLAN_STATUS_TEXT = {
        0: "已删除", 1: "投放中", 2: "已暂停", 3: "已超预算",
        4: "已删除", 5: "审核中", 6: "审核不通过", 7: "已更新", 8: "断投",
    }
    # 千川标准计划状态（英文枚举）→ 中文
    _PLAN_STATUS_STD = {
        "DELIVERY_OK": "投放中", "DISABLE": "已暂停", "FROZEN": "冻结",
        "DELETED": "已删除", "AUDIT": "审核中", "OFFLINE_AUDIT": "审核中",
        "REAUDIT": "审核不通过", "OFFLINE_BUDGET": "余额不足", "OFFLINE_BALANCE": "余额不足",
        "NO_SCHEDULE": "未开始", "TIME_DONE": "已结束", "TIME_NO_REACH": "未开始",
        "SYSTEM_DISABLE": "系统暂停", "QUOTA_DISABLE": "配额受限",
        "LIVE_ROOM_OFF": "未开播", "EXTERNAL_URL_DISABLE": "链接失效",
        "ROI2_DISABLE": "ROI不达标",
    }

    def _plan_status_text(self, status):
        if status is None:
            return "未知"
        if isinstance(status, int) or (isinstance(status, str) and status.isdigit()):
            return self._PLAN_STATUS_TEXT.get(int(status), f"状态{status}")
        return self._PLAN_STATUS_STD.get(str(status), f"状态{status}")

    def get_all_plans_summary(self, force: bool = False) -> dict:
        """拉取该广告主（店铺）下的【投放中】投放计划，并汇总预算投放情况。
        只返回投放中的计划（全域与标准计划均按 DELIVERY_OK 过滤），冻结/暂停/删除等不计入。
        覆盖两类来源：
          - 全域推广-商品（uni_promotion/list，marketing_goal=VIDEO_PROM_GOODS）
          - 标准推广（ad/get：VIDEO_PROM_GOODS 短视频带货 / LIVE_PROM_GOODS 直播带货）
        返回：{
          plan_count: 计划总数（仅投放中）,
          plans: [{ad_id, name, status, status_text, budget(日预算), cost(90天消耗)}],
          total_budget: 投放总量（投放中计划日预算合计）,
          total_cost: 已消耗合计,
          remain_budget: 剩余投放量 = 总量 - 已消耗,
        }
        两类列表接口都直接返回日预算（无需详情），消耗走计划报表。结果缓存 5 分钟。"""
        cache_key = ("plans_summary", str(self.advertiser_id))
        if not force:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        now = datetime.datetime.now()
        today = datetime.date.today()
        start_s = (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d 00:00:00")
        end_s = now.strftime("%Y-%m-%d 23:59:59")
        start_d = (today - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        end_d = today.strftime("%Y-%m-%d")

        plans = {}  # ad_id -> {"ad_id","name","status","budget"}

        def _get_json(url, params, retries=2):
            """千川接口偶发限流/网络抖动：失败时重试，避免整个汇总返回 0。"""
            for attempt in range(retries):
                try:
                    j = requests.get(url, headers=self.headers, params=params, timeout=30).json()
                    if j.get("code") == 0:
                        return j
                except Exception:
                    pass
                if attempt < retries - 1:
                    _time.sleep(0.5)
            return None

        # 1. 全域推广计划（商品全域 UNI_PROJECT，千川侧直接过滤只取投放中 DELIVERY_OK）
        #    注意：adlab_scene 传 OVERALL_PROJECT 会查不到（正确场景值 UNI_PROJECT，不传即可）；
        #    ad_info 直接返回 budget（日预算），无需再调详情
        url = f"{self.base_url}/open_api/v1.0/qianchuan/uni_promotion/list/"
        page = 1
        while True:
            params = {
                "advertiser_id": int(self.advertiser_id),
                "start_time": start_s, "end_time": end_s,
                "marketing_goal": "VIDEO_PROM_GOODS",
                "filtering": json.dumps({"status": "DELIVERY_OK"}),
                "fields": json.dumps(["stat_cost"]),
                "page": page, "page_size": 100,
            }
            j = _get_json(url, params)
            if j is None:
                break
            data = j.get("data", {}) or {}
            ad_list = data.get("ad_list") or []
            for p in ad_list:
                info = p.get("ad_info", {}) or {}
                ad_id = str(info.get("id"))
                if not ad_id:
                    continue
                # stats_info.stat_cost 单位是微元（1e-6 元），换算成元
                st = p.get("stats_info", {}) or {}
                raw_cost = float(st.get("stat_cost") or 0)
                plans[ad_id] = {
                    "ad_id": ad_id,
                    "name": info.get("name") or f"计划{ad_id}",
                    "status": info.get("status"),
                    "budget": float(info.get("budget") or 0),
                    "cost": round(raw_cost / 1e6, 2),
                }
            page_info = data.get("page_info", {}) or {}
            total_page = int(page_info.get("total_page") or 1)
            if page >= total_page or not ad_list:
                break
            page += 1

        # 2. 标准推广计划（短视频带货 + 直播带货，千川侧直接过滤只取投放中 DELIVERY_OK），预算随列表返回
        for mg in ("VIDEO_PROM_GOODS", "LIVE_PROM_GOODS"):
            page = 1
            while True:
                params = {
                    "advertiser_id": int(self.advertiser_id),
                    "start_time": start_d, "end_time": end_d,
                    "filtering": json.dumps({"marketing_goal": mg, "status": "DELIVERY_OK"}),
                    "page": page, "page_size": 100,
                }
                j = _get_json(f"{self.base_url}/open_api/v1.0/qianchuan/ad/get/", params)
                if j is None:
                    break
                data = j.get("data", {}) or {}
                lst = data.get("list") or []
                for row in lst:
                    ad_id = str(row.get("ad_id"))
                    if not ad_id:
                        continue
                    ds = row.get("delivery_setting", {}) or {}
                    plans[ad_id] = {
                        "ad_id": ad_id,
                        "name": row.get("name") or f"计划{ad_id}",
                        "status": row.get("status"),
                        "budget": float(ds.get("budget") or 0),
                    }
                page_info = data.get("page_info", {}) or {}
                total_page = int(page_info.get("total_page") or 1)
                if page >= total_page or not lst:
                    break
                page += 1

        # 3. 计划维度报表拉消耗（90天，一次请求覆盖全部计划）
        costs = {}
        try:
            for item in self.get_ad_report_data(start_date=start_d, end_date=end_d):
                if item.ad_plan_id:
                    costs[str(item.ad_plan_id)] = round(float(item.cost or 0), 2)
        except Exception:
            pass

        plan_items = [{
            "ad_id": p["ad_id"],
            "name": p["name"],
            "status": p["status"],
            "status_text": self._plan_status_text(p["status"]),
            "budget": round(float(p["budget"] or 0), 2),
            "cost": costs.get(p["ad_id"]) or p.get("cost", 0.0),
        } for p in plans.values()]

        total_budget = round(sum(x["budget"] for x in plan_items), 2)
        total_cost = round(sum(x["cost"] for x in plan_items), 2)
        out = {
            "plan_count": len(plan_items),
            "plans": plan_items,
            "total_budget": total_budget,
            "total_cost": total_cost,
            "remain_budget": round(total_budget - total_cost, 2),
        }
        _cache_set(cache_key, out, 300)
        return out

    def _fetch_report_page(self, params) -> Optional[dict]:
        """拉一页报表数据，全局信号量限制并发数，返回原始 JSON 或 None。"""
        with _REPORT_SEM:
            try:
                r = requests.get(
                    f"{self.base_url}/open_api/v1.0/qianchuan/report/uni_promotion/data/get/",
                    headers=self.headers, params=params, timeout=30)
                return r.json()
            except Exception:
                return None

    def _fetch_report_topic(self, topic, dims, filters, start_s, end_s, page_size=100,
                            metrics=None, data_period=None, granularity=None, order_field=None):
        """拉取单个主题的全部页：先取第1页拿 total_page，再并发取剩余页。
        metrics 默认用素材报表指标集；data_period 乘方主题传 OVER_ALL_DATA；granularity 传 TIME_GRANULARITY_HOURLY 可拿分时；
        order_field 覆盖默认排序字段（部分主题无 stat_cost_for_roi2 指标）。"""
        metrics = metrics or ["stat_cost_for_roi2", "product_show_count_for_roi2",
                              "product_click_count_for_roi2", "product_cvr_rate_for_roi2",
                              "product_convert_rate_for_roi2", "total_pay_order_count_for_roi2",
                              "total_pay_order_gmv_for_roi2", "total_prepay_and_pay_order_roi2"]  # 列表快查用基础8项
        order_field = order_field or "stat_cost_for_roi2"

        def _params(page):
            p = {
                "advertiser_id": int(self.advertiser_id),
                "data_topic": topic,
                "dimensions": json.dumps(dims),
                "metrics": json.dumps(metrics),
                "filters": json.dumps(filters),
                "start_time": start_s, "end_time": end_s,
                "order_by": json.dumps([{"type": 2, "field": order_field}]),
                "page": page, "page_size": page_size,
            }
            if data_period:
                p["data_period"] = data_period
            if granularity:
                p["time_granularity"] = granularity
            return p

        first = self._fetch_report_page(_params(1))
        if not first or first.get("code") != 0:
            return []
        data = first.get("data", {}) or {}
        rows = list(data.get("rows") or [])
        total_page = (data.get("page_info", {}) or {}).get("total_page", 1)
        if total_page > 1:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(self._fetch_report_page, _params(p))
                           for p in range(2, total_page + 1)]
                for fu in as_completed(futures):
                    rj = fu.result()
                    if rj and rj.get("code") == 0:
                        rows.extend((rj.get("data", {}) or {}).get("rows", []) or [])
        return rows

    def get_all_materials_report(self, force: bool = False) -> list:
        """账户级素材报表：拉本账户「商品全域/乘方」素材库里全部素材投放数据（近3个月）。
        对应千川后台 数据-素材数据-素材分析（推商品）。
        接口 /qianchuan/report/uni_promotion/data/get/，按素材类型分主题循环翻页。
        已优化：5 类主题并行拉取 + 页内并发翻页 + 5 分钟 TTL 缓存；force=True 强制绕过缓存重拉。
        返回 [{plan_name, materials:[{type,id,name,消耗,展示,点击,点击率,转化率,...}]}]"""
        import datetime as _dt
        cache_key = ("report", str(self.advertiser_id))
        if not force:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        end = _dt.date.today()
        start = end - _dt.timedelta(days=90)
        start_s = start.strftime("%Y-%m-%d") + " 00:00:00"
        end_s = end.strftime("%Y-%m-%d") + " 23:59:59"

        # 素材类型 -> (数据主题, 名称维度, 是否有material_id维度)
        # 视频: name=roi2_material_video_name; 图片: name=roi2_material_image_name; 标题: 仅 roi2_title_material_v3
        tasks = []
        for tname, topic, name_dim, has_mid in [
            ("视频", "SITE_PROMOTION_PRODUCT_POST_DATA_VIDEO", "roi2_material_video_name", True),
            ("图片", "SITE_PROMOTION_PRODUCT_POST_DATA_IMAGE", "roi2_material_image_name", True),
            ("标题", "SITE_PROMOTION_PRODUCT_POST_DATA_TITLE", "roi2_title_material_v3", False),
        ]:
            dims = (["material_id", name_dim] if has_mid else [name_dim])
            tasks.append((tname, topic, dims, [], name_dim, has_mid))
        # 乘方(乘方新升级)主题素材（我们自己建的乘方计划素材在这里）
        for tv, tname in [("3", "视频"), ("2", "图片")]:
            tasks.append((
                tname, "OVERALL_ROI_PRODUCT_MATERIAL",
                ["material_id", "roi2_material_video_name"],
                [{"field": "roi2_material_type_v3", "operator": 7, "values": [tv]}],
                "roi2_material_video_name", True,
            ))

        mats = []
        seen = set()

        def _parse(row, tname, name_dim, has_mid):
            dim = row.get("dimensions", {}) or {}
            met = row.get("metrics", {}) or {}

            def v(sec, k):
                node = (sec.get(k) or {})
                return node.get("Value", node.get("ValueStr", 0))

            cost = float(v(met, "stat_cost_for_roi2") or 0)
            show = int(float(v(met, "product_show_count_for_roi2") or 0))
            click = int(float(v(met, "product_click_count_for_roi2") or 0))
            ctr = float(v(met, "product_cvr_rate_for_roi2") or 0)
            cvr = float(v(met, "product_convert_rate_for_roi2") or 0)
            orders = int(float(v(met, "total_pay_order_count_for_roi2") or 0))
            gmv = float(v(met, "total_pay_order_gmv_for_roi2") or 0)
            roi = float(v(met, "total_prepay_and_pay_order_roi2") or 0)
            if has_mid:
                mid = str(v(dim, "material_id") or "")
            else:
                mid = "T" + str(v(dim, name_dim) or "")
            nm = str(v(dim, name_dim) or "")
            # 报表接口返回的全部指标（含未单独解析的字段），按接口顺序保留
            metrics_all = []
            for _k, _node in (met or {}).items():
                _val = _node.get("Value", _node.get("ValueStr", 0))
                metrics_all.append({
                    "cn": _METRIC_CN.get(_k, _k), "field": _k, "value": _val,
                })
            return {
                "type": tname, "id": mid,
                "name": nm or ("素材" + mid),
                "消耗": round(cost, 2),
                "展示": show, "点击": click,
                "点击率": round(ctr, 2), "转化率": round(cvr, 2),
                "成交单数": orders, "成交金额": round(gmv, 2),
                "支付ROI": round(roi, 2), "净成交ROI": round(roi, 2),
                "退款率(%)": 0.0,
                "metrics_all": metrics_all,
            }

        with ThreadPoolExecutor(max_workers=min(5, len(tasks))) as ex:
            futures = {ex.submit(self._fetch_report_topic, topic, dims, filters, start_s, end_s): (tname, name_dim, has_mid)
                       for tname, topic, dims, filters, name_dim, has_mid in tasks}
            for fu in as_completed(futures):
                tname, name_dim, has_mid = futures[fu]
                for row in fu.result():
                    item = _parse(row, tname, name_dim, has_mid)
                    if item["id"] in seen:
                        continue  # 去重（乘方与全域可能重复）
                    seen.add(item["id"])
                    mats.append(item)

        # 按消耗降序
        mats.sort(key=lambda x: x["消耗"], reverse=True)
        result = [{"plan_name": "账户素材库", "status": "", "materials": mats}]
        _cache_set(cache_key, result, 300)
        return result

    def funnel_optimize(self, materials: list) -> list:
        """多维度漏斗诊断算法：曝光->点击->转化->成交/ROI，逐素材定位瓶颈、打分、给动作。
        阈值按童装非标电商经验设定：CTR 优质>3%/合格1.5-3%/差<1%；CVR 优质>3%/合格1-3%/差<1%。"""
        target_roi = float(DOUYIN_CONFIG.get("ROI_GOAL", 1.5))
        result = []
        for m in materials:
            for x in m["materials"]:
                cost = x["消耗"]; show = x["展示"]; click = x["点击"]
                ctr = x["点击率"]; cvr = x["转化率"]
                orders = x["成交单数"]; gmv = x["成交金额"]; roi = x["支付ROI"]
                settle_roi = x.get("净成交ROI", roi)
                refund_rate = x.get("退款率(%)", 0)
                cost_per_order = x.get("成交成本", 0)
                problems, actions = [], []
                score = 100
                # 样本量判断：展示<500 或 消耗<50 视为冷启动，不轻易判死
                cold = (show < 500) or (cost < 50)

                # 漏斗第1层：曝光->点击（素材吸引力）
                if show >= 200:
                    if ctr < 1:
                        problems.append("点击率<1%，封面/前3秒完全不抓眼球")
                        actions.append("换封面+重做前3秒钩子（痛点/价格/反差开头）")
                        score -= 35
                    elif ctr < 1.5:
                        problems.append("点击率偏弱(1-1.5%)，前3秒钩子不足")
                        actions.append("前3秒改痛点开场或价格利益点")
                        score -= 15
                    elif ctr >= 3:
                        actions.append("素材引流能力OK，可作为主力素材")

                # 漏斗第2层：点击->转化（商品承接）
                if click >= 10:
                    if cvr < 1:
                        problems.append("有点击但转化率<1%，问题在商品承接而非素材")
                        actions.append("优化商品主图/详情/价格/评价，或检查佣金与库存")
                        score -= 25
                    elif cvr < 3:
                        problems.append("转化率中等(1-3%)，承接一般")
                        score -= 8

                # 漏斗第3层：成交/投产（用净成交ROI，更准；并看退款风险）
                if cost >= 50:
                    if orders == 0 and cost >= 100:
                        problems.append(f"已烧{cost}元零成交")
                        actions.append("暂停加预算，排查商品与定向")
                        score -= 30
                    if settle_roi == 0 and cost >= 100:
                        actions.append("建议关停该素材")
                    elif settle_roi < 0.5:
                        problems.append(f"净成交ROI={settle_roi} 严重亏损")
                        actions.append("降价/收窄定向或关停")
                        score -= 25
                    elif settle_roi < target_roi:
                        problems.append(f"净成交ROI={settle_roi} 低于目标{target_roi}")
                        actions.append("观察或微调出价/ROI目标")
                        score -= 10
                    else:
                        actions.append(f"净成交ROI={settle_roi}达标，可加预算放量")
                        score += 5
                    # 成交成本
                    if cost_per_order > 0 and orders >= 3:
                        problems.append(f"单笔成交成本{cost_per_order}元")
                    # 退款风险层
                    if refund_rate >= 30:
                        problems.append(f"退款率{refund_rate}%偏高，成交可能是虚的")
                        actions.append("查商品质量/描述不符/物流，勿放量")
                        score -= 15
                    elif refund_rate >= 15:
                        problems.append(f"退款率{refund_rate}%略高")
                        score -= 6

                # 结论分级
                if cold and not problems:
                    verdict = "冷启动观察"
                elif score >= 80 and settle_roi >= target_roi:
                    verdict = "放量"
                elif score <= 40:
                    verdict = "关停/重做"
                elif any("商品承接" in p for p in problems):
                    verdict = "查商品承接，勿盲目加素材"
                else:
                    verdict = "优化后观察"

                result.append({
                    "计划": m["plan_name"],
                    "素材": x["name"],
                    "类型": x["type"],
                    "商品名": x.get("商品名", ""),
                    "预览图": x.get("预览图", ""),
                    "商品图": x.get("商品图", ""),
                    "漏斗": f"展示{show} → 点击{click}(CTR{ctr}%) → 转化(CVR{cvr}%) → 成交{orders}单/{gmv}元",
                    "消耗": cost, "支付ROI": roi, "净成交ROI": settle_roi,
                    "退款率(%)": refund_rate,
                    "样本充足": not cold,
                    "问题": problems or ["数据样本不足，暂无明显问题"],
                    "建议": actions or ["继续跑量观察"],
                    "评分": max(0, min(100, score)),
                    "结论": verdict,
                })
        # 按评分升序（最差的在前）
        result.sort(key=lambda r: r["评分"])
        return result

    def ai_analyze_materials(self) -> dict:
        """汇总素材投放数据，用漏斗算法诊断，再调 DeepSeek 给策略建议。"""
        materials = self.get_all_materials_report()
        diagnosis = self.funnel_optimize(materials)
        # 拼数据文本给 AI（素材已按消耗降序，只取消耗最高的前50个，避免prompt过大）
        TOP_N = 50
        lines = []
        for i, m in enumerate(materials, 1):
            ms = m["materials"]
            if not ms:
                lines.append(f"{i}. 计划[{m['plan_name']}] 状态{m['status']}：近3个月无消耗素材")
                continue
            shown = ms[:TOP_N]
            lines.append(f"{i}. 计划[{m['plan_name']}] 状态{m['status']}（共{len(ms)}个素材，以下为消耗前{len(shown)}）")
            for k, x in enumerate(shown, 1):
                lines.append(
                    f"   素材{k}[{x['type']}] {x['name']}（商品:{x.get('商品名','')}，审核:{x.get('审核','-')}）: "
                    f"消耗{x['消耗']}元, 展示{x['展示']}, 点击{x['点击']}, "
                    f"点击率{x['点击率']}%, 转化率{x['转化率']}%, "
                    f"成交{x['成交单数']}单/{x['成交金额']}元, 支付ROI {x['支付ROI']}"
                )
        data_text = "\n".join(lines) if lines else "（近3个月无投放数据）"

        # 算法已给出的结构化诊断，喂给 AI（diagnosis按评分升序，最差的在前，取前30）
        algo_lines = []
        for d in diagnosis[:30]:
            algo_lines.append(
                f"- 素材[{d['素材']}]({d['类型']}) 结论:{d['结论']} 评分:{d['评分']} | "
                f"瓶颈:{'; '.join(d['问题'])} | 建议:{'; '.join(d['建议'])}"
            )
        algo_text = "\n".join(algo_lines) or "（无）"

        prompt = (
            "你是巨量千川素材优化专家。下面是某童装店铺账户素材近3个月投放数据，以及基于"
            "「曝光→点击→转化→成交」漏斗算法的结构化诊断（按评分升序，越靠前越差）。\n"
            "注意：你看不到素材画面，只能依据漏斗数据推断问题。\n"
            "请重点分析【图片、视频、图文/标题等其它素材】各自存在的问题，并给出可落地的改法：\n"
            "A. 视频类素材：看CTR判断前3秒/封面/钩子是否抓人，看完播倾向判断节奏；"
            "CTR低就给前3秒脚本方向（痛点/价格/反差/使用场景开头）、字幕/BGM/节奏怎么改；\n"
            "B. 图片类素材：CTR低就给构图、卖点字、配色、模特/角度、价格利益点怎么改；\n"
            "C. 图文/标题等其它素材：根据其CTR/CVR给具体优化方向；\n"
            "D. 若某素材CTR高但CVR/ROI差，明确指出这是商品承接问题不是素材问题，别再改素材；\n"
            "E. 哪些素材直接关停、哪些值得做替身、新素材应拍什么方向。\n"
            "输出结构：先一句话总体判断，然后按【视频】【图片】【其它素材】三类分别逐条给问题+改法，"
            "最后一份按优先级排的行动清单。用中文，简洁专业，不要复述原始数字。\n\n"
            f"【漏斗数据】\n{data_text}\n\n"
            f"【算法诊断】\n{algo_text}"
        )

        api_key = DOUYIN_CONFIG.get("DEEPSEEK_API_KEY", "")
        base = DOUYIN_CONFIG.get("DEEPSEEK_BASE", "https://api.deepseek.com")
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "你是资深千川投放优化师，擅长素材诊断。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.5,
            },
            timeout=90,
        )
        rj = resp.json()
        ai_text = ""
        if resp.status_code == 200 and rj.get("choices"):
            ai_text = rj["choices"][0]["message"]["content"]
        else:
            ai_text = f"DeepSeek 调用失败: HTTP {resp.status_code} {rj}"
        return {"materials": materials, "diagnosis": diagnosis, "ai": ai_text}

    # ---- 单素材详情 ----
    _TYPE_TOPIC = {
        "视频": "SITE_PROMOTION_PRODUCT_POST_DATA_VIDEO",
        "图片": "SITE_PROMOTION_PRODUCT_POST_DATA_IMAGE",
        "标题": "SITE_PROMOTION_PRODUCT_POST_DATA_TITLE",
        "其他": "SITE_PROMOTION_PRODUCT_POST_DATA_OTHER",
    }

    def get_material_detail(self, material_id: str) -> dict:
        """单个素材：汇总指标 + 逐日投放曲线 + DeepSeek 点评与修改建议。
        汇总行来自素材报表缓存（≤5分钟），逐日曲线实时拉取，仅 DeepSeek 点评缓存 10 分钟（省钱）。"""
        import datetime as _dt
        end = _dt.date.today()
        start = end - _dt.timedelta(days=90)
        start_s = start.strftime("%Y-%m-%d") + " 00:00:00"
        end_s = end.strftime("%Y-%m-%d") + " 23:59:59"

        # 1. 找到汇总行
        mats = self.get_all_materials_report()[0]["materials"]
        row = next((m for m in mats if m["id"] == str(material_id)), None)
        if not row:
            raise Exception(f"素材 {material_id} 近3个月无投放数据")
        topic = self._TYPE_TOPIC.get(row["type"], "SITE_PROMOTION_PRODUCT_POST_DATA_VIDEO")

        # 2. 逐日曲线（标题类无material_id，跳过）
        # 素材可能来自全域(SITE_...)或乘方(OVERALL_ROI_PRODUCT_MATERIAL)，两个主题都试一遍
        daily = []
        name_dim = {"视频": "roi2_material_video_name", "图片": "roi2_material_image_name"}.get(row["type"])
        if name_dim:
            candidate_topics = [topic]
            if "OVERALL_ROI_PRODUCT_MATERIAL" not in candidate_topics:
                candidate_topics.append("OVERALL_ROI_PRODUCT_MATERIAL")
            for topic_try in candidate_topics:
                try:
                    nd = name_dim if topic_try.startswith("SITE") else "roi2_material_video_name"
                    p = {
                        "advertiser_id": int(self.advertiser_id), "data_topic": topic_try,
                        "dimensions": json.dumps(["stat_time_day", "material_id", nd]),
                        "metrics": json.dumps(["stat_cost_for_roi2", "product_show_count_for_roi2",
                            "product_click_count_for_roi2", "product_cvr_rate_for_roi2",
                            "product_convert_rate_for_roi2", "total_pay_order_count_for_roi2",
                            "total_pay_order_gmv_for_roi2"]),
                        "filters": json.dumps([{"field": "material_id", "operator": 7, "values": [str(material_id)]}]),
                        "start_time": start_s, "end_time": end_s,
                        "order_by": json.dumps([{"type": 1, "field": "stat_time_day"}]),
                        "page": 1, "page_size": 200,
                    }
                    rj = requests.get(
                        f"{self.base_url}/open_api/v1.0/qianchuan/report/uni_promotion/data/get/",
                        headers=self.headers, params=p, timeout=30).json()
                    if rj.get("code") == 0:
                        for row_d in (rj.get("data", {}) or {}).get("rows", []):
                            dim = row_d.get("dimensions", {}) or {}
                            met = row_d.get("metrics", {}) or {}
                            def v(sec, k):
                                node = (sec.get(k) or {})
                                return node.get("Value", node.get("ValueStr", 0))
                            _day_node = dim.get("stat_time_day") or {}
                            _day_raw = _day_node.get("ValueStr") or _day_node.get("Value") or ""
                            if str(_day_raw).isdigit():
                                import datetime as _dt2
                                day_str = _dt2.datetime.fromtimestamp(int(_day_raw)).strftime("%Y-%m-%d")
                            else:
                                day_str = str(_day_raw)[:10]
                            daily.append({
                                "date": day_str,
                            "cost": round(float(v(met, "stat_cost_for_roi2") or 0), 2),
                            "show": int(float(v(met, "product_show_count_for_roi2") or 0)),
                            "click": int(float(v(met, "product_click_count_for_roi2") or 0)),
                            "orders": int(float(v(met, "total_pay_order_count_for_roi2") or 0)),
                            "gmv": round(float(v(met, "total_pay_order_gmv_for_roi2") or 0), 2),
                        })
                except Exception:
                    pass
                if daily:
                    break

        # 3. 投放时间画像
        active_days = len(daily)
        first_day = daily[0]["date"] if daily else ""
        last_day = daily[-1]["date"] if daily else ""
        peak = max(daily, key=lambda x: x["cost"]) if daily else None
        # 近7天趋势
        recent7 = daily[-7:] if daily else []
        recent7_cost = round(sum(x["cost"] for x in recent7), 2)
        prev7 = daily[-14:-7] if len(daily) >= 7 else []
        prev7_cost = round(sum(x["cost"] for x in prev7), 2)
        trend_dir = "上升" if recent7_cost > prev7_cost * 1.15 else ("下滑" if recent7_cost < prev7_cost * 0.85 else "平稳")

        summary = {
            **row,
            "active_days": active_days, "first_day": first_day, "last_day": last_day,
            "peak_day": peak["date"] if peak else "", "peak_cost": peak["cost"] if peak else 0,
            "recent7_cost": recent7_cost, "prev7_cost": prev7_cost, "trend": trend_dir,
        }

        # 2.5 全量指标（25 项）：按素材过滤单独拉一次报表，覆盖列表的 8 项基础指标。
        # 只查单素材 + 两个主题，秒回；未命中时保留列表的基础 8 项。
        try:
            full_met = {}
            for topic_try in [topic] + (["OVERALL_ROI_PRODUCT_MATERIAL"] if topic != "OVERALL_ROI_PRODUCT_MATERIAL" else []):
                nd = name_dim if topic_try.startswith("SITE") else "roi2_material_video_name"
                p = {
                    "advertiser_id": int(self.advertiser_id), "data_topic": topic_try,
                    "dimensions": json.dumps(["material_id", nd]),
                    "metrics": json.dumps(_MATERIAL_STATS_FIELDS),
                    "filters": json.dumps([{"field": "material_id", "operator": 7,
                                            "values": [str(material_id)]}]),
                    "start_time": start_s, "end_time": end_s,
                    "order_by": json.dumps([{"type": 2, "field": "stat_cost_for_roi2"}]),
                    "page": 1, "page_size": 100,
                }
                rj = requests.get(
                    f"{self.base_url}/open_api/v1.0/qianchuan/report/uni_promotion/data/get/",
                    headers=self.headers, params=p, timeout=30).json()
                if rj.get("code") == 0:
                    for row_f in (rj.get("data", {}) or {}).get("rows", []) or []:
                        met_f = row_f.get("metrics", {}) or {}
                        for k, node in met_f.items():
                            if k not in full_met:
                                full_met[k] = node.get("Value", node.get("ValueStr", 0))
                if full_met:
                    break
            if full_met:
                summary["metrics_all"] = [
                    {"cn": _METRIC_CN.get(k, k), "field": k, "value": v}
                    for k, v in full_met.items()
                ]
        except Exception:
            pass

        # 4. DeepSeek 单素材点评（缓存10分钟，避免重复调用花钱）
        ai_key = ("ai", str(self.advertiser_id), str(material_id))
        ai_text = _cache_get(ai_key)
        if ai_text is None:
            daily_text = "\n".join(
                f"  {d['date']}: 消耗{d['cost']}, 展示{d['show']}, 点击{d['click']}, 成交{d['orders']}单/{d['gmv']}元"
                for d in daily[-30:]
            )
            prompt = (
                "你是资深千川素材优化师。下面是【单个素材】近3个月的投放汇总和逐日消耗曲线。"
                "你看不到素材画面，只能用数据诊断。请输出两部分：\n"
                "一、素材点评：这个素材跑量能力如何（CTR/CVR/ROI/消耗趋势），处在什么阶段（起量/稳定/衰退/冷启动），"
                "问题出在素材本身还是商品承接（CTR高但不成交=商品问题）。\n"
                "二、修改建议：具体可落地的改法——若CTR低，给前3秒/封面/钩子方向；若CVR低，判断是否商品问题；"
                "若在衰退期，建议做什么方向的替身素材；是否值得继续投。用中文，简洁专业，分小标题，不要复述大段原始数字。\n\n"
                f"素材类型：{row['type']}，名称：{row['name']}\n"
                f"汇总：消耗{row['消耗']}元, 展示{row['展示']}, 点击{row['点击']}, "
                f"CTR {row['点击率']}%, CVR {row['转化率']}%, 成交{row['成交单数']}单/{row['成交金额']}元, ROI {row['支付ROI']}\n"
                f"投放时间：累计{active_days}天, 首发{first_day}~{last_day}, 峰值{peak['date'] if peak else ''}消耗{peak['cost'] if peak else 0}元, 近7天趋势{trend_dir}(近7天{recent7_cost}元 vs 前7天{prev7_cost}元)\n"
                f"逐日曲线(近30天):\n{daily_text or '（无逐日数据）'}"
            )
            api_key = DOUYIN_CONFIG.get("DEEPSEEK_API_KEY", "")
            base = DOUYIN_CONFIG.get("DEEPSEEK_BASE", "https://api.deepseek.com")
            try:
                resp = requests.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": "deepseek-chat",
                          "messages": [{"role": "system", "content": "你是资深千川投放优化师。"},
                                       {"role": "user", "content": prompt}],
                          "temperature": 0.5},
                    timeout=90)
                rj = resp.json()
                ai_text = rj["choices"][0]["message"]["content"] if (resp.status_code == 200 and rj.get("choices")) else f"DeepSeek失败:{rj}"
            except Exception as e:
                ai_text = f"DeepSeek调用失败:{e}"
            _cache_set(ai_key, ai_text, 600)

        # 5. 素材预览（视频取播放url+封面，图片取图片url）
        preview = self._get_material_preview(material_id, row["type"])
        return {"summary": summary, "daily": daily, "ai": ai_text, "preview": preview}

    def _fetch_material_lib(self, material_id: str, mtype: str) -> dict:
        """按 material_id 取素材库完整信息（URL/封面/名称/审核状态等原始字段）。结果缓存 10 分钟。
        视频用 /qianchuan/video/get/，图片用 /qianchuan/image/get/；标题类无素材库记录。"""
        mid = str(material_id)
        if not mid.isdigit():
            return {"id": mid, "type": mtype, "note": "标题类素材无素材库记录"}
        cache_key = ("matlib", str(self.advertiser_id), mid, mtype)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        path = ("/open_api/v1.0/qianchuan/image/get/" if mtype == "图片"
                else "/open_api/v1.0/qianchuan/video/get/")
        # 限流(40100)时退避重试
        rj = {}
        for _ in range(4):
            try:
                rj = requests.get(
                    f"{self.base_url}{path}", headers=self.headers,
                    params={"advertiser_id": int(self.advertiser_id),
                            "filtering": json.dumps({"material_ids": [int(mid)]})},
                    timeout=30).json()
            except Exception:
                rj = {}
            if rj.get("code") == 0:
                break
            if rj.get("code") == 40100:
                import time as _t
                _t.sleep(3)
                continue
            break
        result = {"id": mid, "type": mtype}
        try:
            if rj.get("code") == 0:
                lst = rj.get("data", {}).get("list", []) or []
                if lst:
                    item = dict(lst[0])
                    item["kind"] = "image" if mtype == "图片" else "video"
                    item.setdefault("url", item.get("image_url") or item.get("url") or "")
                    result = item
        except Exception:
            pass
        _cache_set(cache_key, result, 600)
        return result

    def _get_material_preview(self, material_id: str, mtype: str) -> dict:
        """按 material_id 取素材预览地址（从素材库信息缓存中提取 url/poster）。"""
        lib = self._fetch_material_lib(material_id, mtype)
        if lib.get("kind") == "video":
            return {"kind": "video", "url": lib.get("url", ""), "poster": lib.get("poster_url", "")}
        if lib.get("kind") == "image":
            return {"kind": "image", "url": lib.get("url", "")}
        return {}

    def get_material_plan_map(self, force: bool = False) -> dict:
        """素材→在投计划映射：遍历在投全域计划详情，找出每个素材挂在哪些计划下。
        返回 {material_id: [{plan_id, plan_name, status, product_id}]}，结果缓存 10 分钟。"""
        cache_key = ("mat_plan_map", str(self.advertiser_id))
        if not force:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached
        url = f"{self.base_url}/open_api/v1.0/qianchuan/uni_promotion/list/"
        params = {
            "advertiser_id": int(self.advertiser_id),
            "start_time": (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d 00:00:00"),
            "end_time": datetime.datetime.now().strftime("%Y-%m-%d 23:59:59"),
            "marketing_goal": "VIDEO_PROM_GOODS",
            "filtering": json.dumps({"status": "DELIVERY_OK"}),
            "fields": json.dumps(["stat_cost"]),
            "page": 1,
            "page_size": 100,
        }
        result = {}
        try:
            j = requests.get(url, headers=self.headers, params=params, timeout=30).json()
        except Exception:
            return result
        if j.get("code") != 0:
            return result
        plan_list = []
        for p in (j.get("data", {}) or {}).get("ad_list", []) or []:
            info = p.get("ad_info", {}) or {}
            if info.get("id"):
                plan_list.append((str(info["id"]), info.get("status", "")))

        def _scan(ad_id, status):
            try:
                d = requests.get(f"{self.base_url}/open_api/v1.0/qianchuan/uni_promotion/ad/detail/",
                                 headers=self.headers,
                                 params={"advertiser_id": int(self.advertiser_id), "ad_id": int(ad_id)},
                                 timeout=8).json()
                if d.get("code") != 0:
                    return []
                data = d.get("data", {}) or {}
                plan_name = data.get("name") or f"计划{ad_id}"
                out = []
                for c in data.get("multi_product_creative_list", []) or []:
                    pid = str(c.get("product_id") or "")
                    for key in ("video_material", "image_material"):
                        for v in c.get(key) or []:
                            vid = str(v.get("video_id") or v.get("image_id") or "")
                            if vid:
                                out.append((vid, {"plan_id": ad_id, "plan_name": plan_name,
                                                  "status": status, "product_id": pid}))
                return out
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=15) as ex:
            futures = [ex.submit(_scan, ad_id, status) for ad_id, status in plan_list]
            for fu in as_completed(futures):
                for vid, info in fu.result():
                    result.setdefault(vid, []).append(info)
        # 按 (plan_id, product_id) 去重
        for vid in result:
            seen, merged = set(), []
            for x in result[vid]:
                k = (x["plan_id"], x["product_id"])
                if k in seen:
                    continue
                seen.add(k)
                merged.append(x)
            result[vid] = merged
        _cache_set(cache_key, result, 600)
        return result

    def get_material_full_data(self, material_id: str) -> dict:
        """单个素材全量数据导出：素材库信息 + 汇总指标 + 逐日曲线 + 关联计划/商品 + AI 点评。"""
        detail = self.get_material_detail(material_id)
        row = detail["summary"]
        lib = self._fetch_material_lib(material_id, row["type"])
        plan_map = self.get_material_plan_map()
        plans = plan_map.get(str(material_id), [])
        products = []
        seen_pids = set()
        for pl in plans:
            if pl.get("product_id") and pl["product_id"] not in seen_pids:
                seen_pids.add(pl["product_id"])
                products.append({"product_id": pl["product_id"]})
        return {
            "advertiser_id": str(self.advertiser_id),
            "material_id": str(material_id),
            "material": lib,
            "summary": row,
            "daily": detail["daily"],
            "ai": detail["ai"],
            "preview": detail["preview"],
            "plans": plans,
            "products": products,
        }

    def get_overall_plan_detail(self, ad_id) -> dict:
        """获取单个全域计划详情（含现有创意/视频素材，用于追加素材）。"""
        url = f"{self.base_url}/open_api/v1.0/qianchuan/uni_promotion/ad/detail/"
        params = {"advertiser_id": int(self.advertiser_id), "ad_id": int(ad_id)}
        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(self._friendly_error("查询计划详情失败", resp_json))
        return resp_json.get("data", {}) or {}

    def add_video_to_plan(self, ad_id, local_file_path: str) -> str:
        """往已有的在投全域计划追加一个素材（视频或图片，全量更新，自动保留旧素材）。"""
        # 1. 读现有计划详情
        detail = self.get_overall_plan_detail(ad_id)
        delivery = detail.get("delivery_setting", {}) or {}
        old_creatives = detail.get("multi_product_creative_list", []) or []
        if not old_creatives:
            raise Exception("该计划详情里没有商品创意，无法追加素材")

        # 商品卡标题必须写清品名，先建 product_id -> 商品名 映射
        try:
            name_map = {str(p["id"]): p["name"] for p in self.get_available_products()}
        except Exception:
            name_map = {}

        # 2. 判断素材类型并上传
        ext = local_file_path.rsplit(".", 1)[-1].lower()
        is_image = ext in ["jpg", "jpeg", "png", "bmp", "webp"]
        image_mode = DOUYIN_CONFIG.get("QIANCHUAN_IMAGE_MODE", "VIDEO_VERTICAL")
        new_slot = {}
        tmp_file = None
        if is_image:
            # 图片：中心裁成商品卡方图
            tmp_file = self._to_square_image(local_file_path)
            new_image_id = self.upload_local_media_get_material_id(tmp_file, "image")
            new_slot["image_material"] = [{"image_mode": "SQUARE", "image_ids": [new_image_id]}]
        else:
            video_id = self.upload_local_media_get_material_id(local_file_path, "video")
            cover_id = ""
            cover_tmp = None
            try:
                cover_tmp = self._extract_cover_frame(local_file_path)
                cover_id = self.upload_local_media_get_material_id(cover_tmp, "image")
            finally:
                if cover_tmp and os.path.exists(cover_tmp):
                    try:
                        os.remove(cover_tmp)
                    except OSError:
                        pass
            new_slot["video_material"] = [
                {"image_mode": image_mode, "video_id": video_id, "video_cover_id": cover_id}
            ]

        # 3. 把新素材追加到每个商品创意，保留旧素材
        creatives_out = []
        for c in old_creatives:
            old_vm = [
                {"image_mode": v.get("image_mode", image_mode),
                 "video_id": v["video_id"],
                 "video_cover_id": v.get("video_cover_id", "")}
                for v in (c.get("video_material") or []) if v.get("video_id")
            ]
            old_im = []
            for im in (c.get("image_material") or []):
                for iid in (im.get("image_ids") or []):
                    old_im.append(iid)
            # 千川限制：每个创意包含图片的素材只能传一张，多余图片会触发 40000 校验失败
            old_im = old_im[:1]
            # 标题只保留 title/title_type，丢弃详情返回的 dynamic_words 等空字段（更新接口不接受 null）
            old_tm = [
                {"title": t.get("title"), "title_type": t.get("title_type", "CUSTOM")}
                for t in (c.get("title_material") or []) if t.get("title")
            ]
            if not old_tm:
                old_tm = [{"title": "秋冬新品保暖热销中", "title_type": "CUSTOM"}]
            entry = {
                "product_id": c["product_id"],
                "aweme_uid": c.get("aweme_uid"),
                "creative_type": c.get("creative_type", "PROGRAMMATIC_CREATIVE"),
                "video_material": old_vm,
                "title_material": old_tm,
            }
            # 先把原有商品图带上（无论追加视频还是图片），否则已有的商品卡标题会校验失败
            if old_im:
                entry["image_material"] = [{"image_mode": "SQUARE", "image_ids": old_im}]
            if is_image:
                old_im.append(new_slot["image_material"][0]["image_ids"][0])
                entry["image_material"] = [{"image_mode": "SQUARE", "image_ids": old_im}]
                # 用了商品卡图，必须同时带商品卡标题，且要写清品名
                has_card_title = any(t.get("title_type") == "COMMODITY_CARD" for t in old_tm)
                if not has_card_title:
                    card_text = name_map.get(str(c["product_id"]), "Babycare秋冬新品保暖马甲")
                    entry["title_material"] = old_tm + [{"title": card_text, "title_type": "COMMODITY_CARD"}]
            else:
                entry["video_material"] = old_vm + new_slot["video_material"]
            # 全域有号商家每个创意必须带投放卡片；卡片配图用商品方图
            if old_im:
                entry["creative_card"] = {
                    "promotion_card_title": "视频同款商品",
                    "promotion_card_selling_points": ["秋冬新款加绒保暖"],
                    "promotion_card_image_id": old_im[0],
                    "promotion_card_action_button": "专属优惠",
                }
            creatives_out.append(entry)

        # 清理临时裁图
        if tmp_file and os.path.exists(tmp_file):
            try: os.remove(tmp_file)
            except OSError: pass

        # 4. 全量更新全域计划（用全域编辑接口 uni_aweme/ad/update）
        url = f"{self.base_url}/open_api/v1.0/qianchuan/uni_aweme/ad/update/"
        payload = {
            "advertiser_id": int(self.advertiser_id),
            "ad_id": int(ad_id),
            "name": detail.get("name", f"plan_{ad_id}"),
            "marketing_goal": "VIDEO_PROM_GOODS",
            "product_ids": [c["product_id"] for c in old_creatives],
            "delivery_setting": {
                "smart_bid_type": delivery.get("smart_bid_type", "SMART_BID_CUSTOM"),
                "roi2_goal": delivery.get("roi2_goal"),
                "qcpx_mode": delivery.get("qcpx_mode", "QCPX_MODE_OFF"),
                "budget": delivery.get("budget"),
                "video_schedule_type": delivery.get("video_schedule_type", "SCHEDULE_FROM_NOW"),
                "deep_external_action": delivery.get("deep_external_action", "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI"),
            },
            "multi_product_creative_list": creatives_out,
        }
        resp = requests.post(url, headers=self.headers, json=payload, timeout=60)
        resp_json = resp.json()
        if resp_json.get("code") != 0:
            raise Exception(self._friendly_error("追加素材失败", resp_json))
        return str(resp_json.get("data", {}).get("ad_id", ad_id))
