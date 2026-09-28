# 兼容层：从 services/ 重新导出，保持原有 import 路径不变
from ad.services.token_manager import *
from ad.services.token_manager import DouYinTokenManager, get_token_mgr, token_mgr
