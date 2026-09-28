# ad 包初始化 - 兼容层重新导出
# 保持原有 import 路径不变：from ad.xxx import yyy

from ad._compat.config import *
from ad._compat.db import *
from ad._compat.douyin_api import *
from ad._compat.engine_config import *
from ad._compat.engine_token import *
from ad._compat.file_service import *
from ad._compat.pg_db import *
from ad._compat.token_manager import *
from ad.db.models_legacy import *
