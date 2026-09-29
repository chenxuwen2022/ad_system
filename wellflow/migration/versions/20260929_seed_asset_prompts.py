"""Seed asset-library system prompts (mannequin / outfit / scene).

把模特库、穿搭库、场景库原先硬编码在 mannequins.py / outfit.py / scenes.py 里的
系统提示词与静态生图提示词，作为初始版本灌入提示词管理库（每个库一个分类，原子发布）。

Idempotent — 已存在的 key 会被跳过，可安全重复执行。

Revision ID: f3a7c9e1b5d2
Revises: 20260929_seed_root
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "f3a7c9e1b5d2"
down_revision = "20260929_seed_root"
branch_labels = None
depends_on = None

CATALOG = (
    ("mannequin_optimize", "mannequin", "提示词优化"),
    ("mannequin_auto_tag", "mannequin", "自动打标"),
    ("mannequin_fine_tune", "mannequin", "生图微调-身份维持"),
    ("outfit_recognize", "outfit", "单品识别"),
    ("outfit_cutout", "outfit", "单品抠图"),
    ("outfit_auto_tag", "outfit", "自动打标"),
    ("scene_extract", "scene", "场景提取"),
    ("scene_mosaic", "scene", "原图马赛克"),
    ("scene_auto_tag", "scene", "自动打标"),
)

SEEDS = {
    "mannequin_optimize": (
        "你是一名专业的电商模特图提示词优化专家，负责把用户的原始描述和维度标签整合成一条"
        "结构化、细节丰富的中文提示词，用于 GPT Image 等 AI 图像生成模型。\n\n"
        "规则：\n"
        "1. 保留用户核心意图，不要凭空创造属性。\n"
        "2. 把维度标签自然融入描述。\n"
        "3. 只输出最终的中文提示词本身，不要解释、不要前后缀、不要引号。\n"
        "4. 如果用户提到了具体服装，要自然描述服装的穿着与展示效果。\n"
        "5. 优先使用中文表达；如果某些风格、材质或摄影术语用英文更自然（如 soft lighting、"
        "cinematic、Denim 等），可以保留，但整体提示词应以中文为主。"
    ),
    "mannequin_auto_tag": (
        "你是一个电商模特属性标注专家。根据提供的模特图片，"
        "从给定的维度枚举中选择最合适的值，以 JSON 格式返回。\n\n"
        "规则：\n"
        "1. 每个维度可多选（多值数组），也可以单选。\n"
        "2. 如果某维度无法从图中判断，返回空数组 []。\n"
        "3. 只使用枚举中出现的值，不要自己创造新值。\n"
        "4. 输出必须是一个合法的 JSON 对象，不要带 markdown 代码块标记或其他文字。"
    ),
    "mannequin_fine_tune": "保持模特的面部特征、发型、整体气质与身份不变。",
    "outfit_recognize": (
        "你是专业的电商服饰单品识别专家。\n\n"
        "请从提供的人物全身穿搭照片中，识别身上穿/戴的每一件单品"
        "（上衣、裤装、鞋、包、腰带、眼镜等），并以如下 JSON 格式返回：\n"
        '{"items":[{"name":"单品名(简洁,如 军绿衬衫外套)",'
        '"category":"品类(衬衫/T恤/裤子/鞋/包/配饰 等通用词)",'
        '"color":"颜色(简洁,如 军绿/米白/黑)"}]}\n\n'
        "要求：\n"
        "1. 按从上到下、从外到内排列。\n"
        "2. 不要包含人物本身特征(发型、肤色、身材)。\n"
        "3. 只输出 JSON，不要任何解释。"
    ),
    "outfit_cutout": (
        "要求:只保留这一件单品,主体完整(被遮挡部分合理补全),"
        "背景纯白,居中构图,无阴影、无文字"
    ),
    "outfit_auto_tag": (
        "你是一个电商穿搭属性标注专家。根据提供的穿搭图(平铺总图或原图),"
        "从给定的维度枚举中选择最合适的值,以 JSON 格式返回。\n\n"
        "规则:\n1. 每个维度可多选(多值数组),也可以单选。\n"
        "2. 如果某维度无法从图中判断,返回空数组 []。\n"
        "3. 只使用枚举中出现的值,不要自己创造新值。\n"
        "4. 输出必须是一个合法的 JSON 对象,不要带 markdown 代码块标记或其他文字。"
    ),
    "scene_extract": (
        "移除这张照片中的全部人物，包括人影、人物倒影和随身物品，"
        "用周围的环境内容自然填充被移除区域：延续原有的地形、植被、天空、水面、"
        "建筑与纹理走向，保持光影方向、色调、饱和度、对比度和景深一致。\n"
        "硬性约束：画面中不得残留任何人体部位（头、脸、手、手臂、腿、脚、头发）、"
        "衣物或人物轮廓边缘；不得出现模糊涂抹痕迹、模糊色块、克隆重复纹理、"
        "明显的修补边界或畸变。\n"
        "除人物外，其余内容必须与原图完全一致：不改动构图、不改变视角与焦距、"
        "不调整色调风格、不新增或删除任何景物、不添加文字水印。\n"
        "如果原图本身不含任何人物，则直接原样输出该图，不做任何修改。"
    ),
    "scene_mosaic": (
        "请对输入图执行「叠加马赛克」操作。注意：这不是生成任务。\n"
        "把输入图当成一张底图，你只被允许在上面贴马赛克方块，"
        "底图本身一个像素都不许动。\n"
        "\n"
        "把输入图和输出图并排放在一起看，除了马赛克方块，"
        "两张图必须一模一样，看不出任何差别。任何背景变化、"
        "色调变化、清晰度变化、构图变化，都算任务失败。\n"
        "\n"
        "严厉禁止：\n"
        "· 禁止重新绘制背景、禁止重新生成画面、禁止换一张图；\n"
        "· 禁止改变天空、山脉、树木、建筑、道路、水面、地面的任何形状与位置；\n"
        "· 禁止调整亮度、对比度、饱和度、白平衡、色温；\n"
        "· 禁止锐化、磨皮、美颜、提画质、加滤镜、加景深虚化；\n"
        "· 禁止清除画面里的电线杆、路牌、垃圾桶、车辆、落叶等任何现有物体；\n"
        "· 禁止裁剪、补边、改变分辨率与画面比例；\n"
        "· 禁止添加文字、水印、logo、边框。\n"
        "\n"
        "唯一改动：在下列区域原地叠加标准马赛克，"
        "把区域切成规则方格、每格填平均色，格子要大到无法辨认原内容。\n"
        "① 整个人物的头部（含面部与头发）；\n"
        "② 裸露或可见的皮肤、身体、四肢、手部；\n"
        "③ 人脸倒影、镜中或屏幕中的人脸；\n"
        "④ 证件、车牌、手机号、地址、二维码、快递单、账号信息；\n"
        "⑤ 裸露与性暗示区域、赌博界面、涉毒物品、血腥伤口、武器、政治敏感标识。\n"
        "马赛克必须完整覆盖并略微外扩，不留边、不漏角；"
        "不得用模糊、涂抹、纯色块、贴纸代替马赛克。\n"
        "\n"
        "如果图里没有上述目标，就原样输出输入图，什么都不做。\n"
        "输出尺寸必须与输入图完全相同。"
    ),
    "scene_auto_tag": (
        "你是电商场景标注专家。根据提供的场景图,从给定维度枚举中选择最合适的值,"
        "以 JSON 返回。规则:\n1. 每维可多选,也可以单选。\n"
        "2. 无法判断的维度返回空数组 []。\n"
        "3. 只使用枚举中出现的值,不要自己创造新值。\n"
        "4. 只输出 JSON,不带 markdown 或其他文字。"
    ),
}

_CATEGORY_ORDER = ("mannequin", "outfit", "scene")


def upgrade():
    db = op.get_bind()
    existing = set(db.execute(sa.text("SELECT key FROM prompt_template")).scalars())
    now = datetime.now(timezone.utc)

    for category in _CATEGORY_ORDER:
        pending = [(key, name) for key, item_category, name in CATALOG
                   if item_category == category and key not in existing]
        if not pending:
            continue
        release_id = db.execute(sa.text(
            "SELECT id FROM prompt_release WHERE category = :category LIMIT 1"
        ), {"category": category}).scalar()
        if release_id is None:
            release_id = db.execute(sa.text("""
                INSERT INTO prompt_release (category, number, note, created_at)
                VALUES (:category, 1, :note, :created_at) RETURNING id
            """), {"category": category, "note": "初始版本", "created_at": now}).scalar_one()
        for key, name in pending:
            db.execute(sa.text("""
                INSERT INTO prompt_template (key, category, name, draft_revision_id, is_archived)
                VALUES (:key, :category, :name, NULL, false)
            """), {"key": key, "category": category, "name": name})
            revision_id = db.execute(sa.text("""
                INSERT INTO prompt_revision (template_key, number, content, note, created_at, is_deleted)
                VALUES (:key, 1, :content, :note, :created_at, false) RETURNING id
            """), {"key": key, "content": SEEDS[key], "note": "初始版本", "created_at": now}).scalar_one()
            db.execute(sa.text("UPDATE prompt_template SET draft_revision_id = :revision_id WHERE key = :key"),
                       {"revision_id": revision_id, "key": key})
            db.execute(sa.text("""
                INSERT INTO prompt_release_item (release_id, template_key, revision_id)
                VALUES (:release_id, :key, :revision_id)
            """), {"release_id": release_id, "key": key, "revision_id": revision_id})


def downgrade():
    # 提示词内容为可编辑的用户数据，downgrade 不删除。
    pass