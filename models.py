from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import datetime


class TokenInfo(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    update_time: datetime.datetime


class UploadResp(BaseModel):
    success: bool
    local_file_path: str
    file_name: str
    file_type: str  # image / video


class AdLaunchRequest(BaseModel):
    local_file_path: str
    platform: str  # douyin / jd / taobao
    advertiser_id: Optional[str] = None  # 留空时后台自动查询已授权的广告主
    ad_group_name: Optional[str] = None  # 留空时用 config 默认值
    ad_plan_name: Optional[str] = None   # 留空时用 config 默认值
    creative_name: Optional[str] = None  # 留空时自动用「前缀+文件名」
    budget: Optional[float] = None       # 留空时用 config 默认值
    bid: Optional[float] = None          # 留空时用 config 默认值
    aweme_id: Optional[str] = None       # 投放抖音号ID，留空时取 config.py 的 AWEME_ID
    product_ids: Optional[List[str]] = None  # 投放商品ID列表，留空时取 config.py 的 PRODUCT_IDS
    plan_id: Optional[str] = None        # 指定投放的千川计划ID：素材追加到该计划下，不新建计划
    tags: Optional[List[str]] = None     # 素材标签列表（来自标签设置），投放时给素材打标记
    test_mode: Optional[bool] = False    # 测试模式：不调用千川真实接口，本地模拟整条投放链路


class AdLaunchResult(BaseModel):
    success: bool
    local_file_path: str
    advertiser_id: Optional[str] = None
    material_id: Optional[str] = None
    ad_group_id: Optional[str] = None
    ad_plan_id: Optional[str] = None
    creative_id: Optional[str] = None
    audit_status: Optional[str] = None
    error_msg: Optional[str] = None


class AdReportItem(BaseModel):
    creative_id: str
    ad_plan_id: str
    ad_group_id: str
    show: int
    click: int
    cost: float
    convert: int
    ctr: float
    cpc: float
    cpm: float
    convert_rate: float
    report_date: str
    extra: Dict[str, Any]
