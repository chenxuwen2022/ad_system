-- ==============================================================================
-- 线上数据库清理 SQL
-- 目标：只保留 node1-node3 生图主流程用到的 4 张表
-- ==============================================================================
-- 保留白名单：
--   - task               任务主表
--   - task_event         任务事件审计
--   - task_error_log     任务错误日志
--   - task_image         任务关联图片
--   - alembic_version    Alembic 版本表（必须保留）
-- ==============================================================================

BEGIN;

-- 动态找出所有不在白名单里的表，拼 DROP 语句一次性执行
DO $$
DECLARE
    drop_sql TEXT;
BEGIN
    SELECT string_agg(
        format('DROP TABLE IF EXISTS %I CASCADE', tablename),
        '; '
        ORDER BY tablename
    )
    INTO drop_sql
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tablename NOT IN (
          'task',
          'task_event',
          'task_error_log',
          'task_image',
          'alembic_version'
      );

    IF drop_sql IS NULL THEN
        RAISE NOTICE '数据库已干净，无需清理';
    ELSE
        RAISE NOTICE '即将执行: %', drop_sql;
        EXECUTE drop_sql;
        RAISE NOTICE '清理完成';
    END IF;
END$$;

-- 验证：打印剩余全部表
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;

COMMIT;
