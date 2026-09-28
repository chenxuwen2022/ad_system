import sys
sys.path.insert(0, '.')
from ad.db import engine
from sqlalchemy import text

# 连接数据库
with engine.connect() as conn:
    # 检查 material_id 列是否存在
    result = conn.execute(text("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'material_launch' 
        AND column_name = 'material_id'
    """))
    
    if result.rowcount == 0:
        print('添加 material_id 列...')
        conn.execute(text("ALTER TABLE material_launch ADD COLUMN material_id VARCHAR DEFAULT ''"))
        print('✅ 已添加 material_id 列')
    else:
        print('material_id 列已存在')
    
    # 检查 advertiser_id 列是否存在
    result = conn.execute(text("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'material_launch' 
        AND column_name = 'advertiser_id'
    """))
    
    if result.rowcount == 0:
        print('添加 advertiser_id 列...')
        conn.execute(text("ALTER TABLE material_launch ADD COLUMN advertiser_id VARCHAR DEFAULT ''"))
        print('✅ 已添加 advertiser_id 列')
    else:
        print('advertiser_id 列已存在')
    
    conn.commit()
    print('✅ 数据库表结构更新完成')
