import sys
sys.path.insert(0, '.')

from ad.db import SessionLocal, MaterialMarkDB, MaterialLaunchDB, MaterialTagDB, MaterialCategoryDB, MaterialBizStatusDB

def clean_materials():
    db = SessionLocal()
    try:
        # 删除素材标记
        mark_count = db.query(MaterialMarkDB).delete()
        print(f'已删除 material_mark 记录: {mark_count} 条')
        
        # 删除素材投放历史
        launch_count = db.query(MaterialLaunchDB).delete()
        print(f'已删除 material_launch 记录: {launch_count} 条')
        
        # 删除素材标签
        tag_count = db.query(MaterialTagDB).delete()
        print(f'已删除 material_tag 记录: {tag_count} 条')
        
        # 删除素材分类体系
        cat_count = db.query(MaterialCategoryDB).delete()
        print(f'已删除 material_category 记录: {cat_count} 条')
        
        # 删除素材投放状态标签
        status_count = db.query(MaterialBizStatusDB).delete()
        print(f'已删除 material_biz_status 记录: {status_count} 条')
        
        db.commit()
        print('✅ 素材相关数据清理完成！')
        print('✅ 店铺信息（advertiser_account）已保留')
    except Exception as e:
        db.rollback()
        print(f'❌ 清理失败: {e}')
        raise
    finally:
        db.close()

if __name__ == '__main__':
    clean_materials()
