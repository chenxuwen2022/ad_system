import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 独立加载 ad/.env（与千川配置隔离；敏感项只存 .env，不入库）
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.isfile(_env_file):
    # 用 utf-8-sig 兼容 PowerShell 写入的 BOM，避免第一个键名带上 \ufeff
    with open(_env_file, encoding="utf-8-sig") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# 巨量营销（巨量引擎开放平台）应用配置 —— 与巨量千川(DOUYIN_CONFIG)完全隔离
ENGINE_CONFIG = {
    "APP_ID": os.environ.get("ENGINE_APP_ID", ""),
    "Secret": os.environ.get("ENGINE_SECRET", ""),
    "token_url": "https://api.oceanengine.com/open_api/oauth2/access_token/",
    "refresh_token_url": "https://api.oceanengine.com/open_api/oauth2/refresh_token/",
    "advertiser_get_url": "https://api.oceanengine.com/open_api/oauth2/advertiser/get/",
    # 独立 token 存储文件（不共享千川 token_store.db）
    "TOKEN_DB_PATH": os.path.join(BASE_DIR, "engine_token_store.db"),
}
