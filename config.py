import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_STORAGE_PATH = os.path.join(BASE_DIR, "media_storage")
os.makedirs(MEDIA_STORAGE_PATH, exist_ok=True)

# 加载本地 .env（仅本地生效，已被 .gitignore 排除，不会进入代码仓库）
# 用于提供 QIANCHUAN_SECRET / DEEPSEEK_API_KEY 等敏感配置
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.isfile(_env_file):
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# 巨量千川广告账户配置，填入你自己的信息
# 敏感项（Secret / API Key）一律从环境变量读取，避免写入代码仓库
DOUYIN_CONFIG = {
    "APP_ID": "1875743997699184",
    "Secret": os.environ.get("QIANCHUAN_SECRET", ""),
    "auth_code": "",  # 一次性 auth_code，使用后即失效，需重新生成
    "access_token": "",
    "refresh_token": "",
    "token_url": "https://api.oceanengine.com/open_api/oauth2/access_token/",
    "refresh_token_url": "https://api.oceanengine.com/open_api/oauth2/refresh_token/",
    "advertiser_get_url": "https://api.oceanengine.com/open_api/oauth2/advertiser/get/",
    "DEFAULT_ADVERTISER_ID": "1854728640974923",  # 千川投放账户
    # 千川投放参数（短视频带货必填）
    "AWEME_ID": "1101052921254393",   # 投放抖音号ID
    "PRODUCT_IDS": ["3840806759778353689"],  # 投放商品ID列表
    "ROI_GOAL": 1.5,                    # 支付ROI目标
    "QIANCHUAN_IMAGE_MODE": "VIDEO_VERTICAL",  # 视频素材类型 VIDEO_VERTICAL 竖版 / VIDEO_LARGE 横版
    # 以下投放参数在后台固定，页面无需填写
    "DEFAULT_GROUP_NAME": "SY",
    "DEFAULT_PLAN_NAME": "SY",
    "CREATIVE_PREFIX": "SY",
    "DEFAULT_BUDGET": 400,
    "DEFAULT_BID": 1,
    # DeepSeek AI 素材分析（密钥从环境变量读取）
    "DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY", ""),
    "DEEPSEEK_BASE": "https://api.deepseek.com",
}

JD_CONFIG = {
    "APP_ID": "",
    "Secret": "",
}
