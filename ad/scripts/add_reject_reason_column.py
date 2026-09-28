import sys
sys.path.insert(0, '.')
from ad.db import engine
from sqlalchemy import text

# 连接数据库
with engine.connect() as conn:
    # 检查 reject_reason 列是否存在
    result = conn.execute(text("""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = 'material_launch' 
        AND column_name = 'reject_reason'
    """))
    
    if result.rowcount == 0:
        print('添加 reject_reason 列...')
        conn.execute(text("ALTER TABLE material_launch ADD COLUMN reject_reason VARCHAR DEFAULT ''"))
        print('✅ 已添加 reject_reason 列')
    else:
        print('reject_reason 列已存在')
    
    conn.commit()
    print('✅ 数据库表结构更新完成')
