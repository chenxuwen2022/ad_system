# 兼容层：转发到 _compat/db.py
from ad._compat.db import *
from ad._compat.db import engine, SessionLocal, Base, TokenDB, AdvertiserDB, MaterialTagDB, MaterialCategoryDB, MaterialBizStatusDB, MaterialMarkDB, MaterialLaunchDB, SkuDB, MaterialUploadDB, MATERIAL_BIZ_STATUSES, MATERIAL_BIZ_STATUS_CLASS, init_db, get_db
