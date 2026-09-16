# -*- coding: utf-8 -*-
"""穿搭库官方种子数据(8 套,来自 PM demo 的资产库 -> 参考素材 -> 穿搭库)。

数据来源:PM demo(wellflow-studio)前端 bundle 中 WF-O004~WF-O011 的
名称 / 单品清单(名称/品类/颜色)/ 维度标签 / 一句话描述。
图片文件位于 static/assets/outfits/ 下,文件名与 demo 一致。

被 scripts/migrate_outfit_library.py 引用,单独存在便于复查与补充。
"""

# 图片基础 URL(后端 static 挂载路径)
IMG_BASE = "/static/assets/outfits"


def _img(name: str) -> str:
    return f"{IMG_BASE}/{name}"


# 8 套官方穿搭。字段说明:
#   code/name/desc: 编号/名称/一句话描述
#   img:           平铺总图(同时作为原图与封面,demo 中二者同图)
#   dims:          六组维度(outfitStyle/category/color/material/fit/func),颜色为平铺数组
#   items:         单品数组(id/name/category/color/image)
OFFICIAL_OUTFITS = [
    {
        "code": "WF-O004",
        "name": "藏青短裤凉拖搭配",
        "desc": "藏青短裤与黑色夹趾凉拖组成的两件搭配，以深色搭配米色鞋床，呈现极简、休闲风格。",
        "img": "wf-o004.png",
        "dims": {
            "outfitStyle": ["极简", "休闲"],
            "category": ["裤子", "鞋"],
            "color": ["藏青", "黑", "米"],
            "material": [],
            "fit": ["短款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o004-shorts", "name": "藏青短裤", "category": "裤子", "color": "藏青", "image": _img("wf-o004-shorts.png")},
            {"id": "wf-o004-sandals", "name": "黑色夹趾凉拖", "category": "鞋", "color": "黑", "image": _img("wf-o004-sandals.png")},
        ],
    },
    {
        "code": "WF-O005",
        "name": "米白长裤通勤搭配",
        "desc": "米白长裤、米色系带鞋、卡其手提包与细框眼镜组成的四件搭配，同色系搭配呈现极简、通勤风格。",
        "img": "wf-o005.png",
        "dims": {
            "outfitStyle": ["极简", "通勤", "休闲"],
            "category": ["裤子", "鞋", "包", "配饰"],
            "color": ["米白", "米", "卡其"],
            "material": [],
            "fit": ["长款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o005-trousers", "name": "米白长裤", "category": "裤子", "color": "米白", "image": _img("wf-o005-trousers.png")},
            {"id": "wf-o005-shoes", "name": "米色系带鞋", "category": "鞋", "color": "米", "image": _img("wf-o005-shoes.png")},
            {"id": "wf-o005-bag", "name": "卡其手提包", "category": "包", "color": "卡其", "image": _img("wf-o005-bag.png")},
            {"id": "wf-o005-glasses", "name": "细框眼镜", "category": "配饰", "color": "棕", "image": _img("wf-o005-glasses.png")},
        ],
    },
    {
        "code": "WF-O006",
        "name": "米白长裤凉拖搭配",
        "desc": "米白长裤、黑色交叉带凉拖、卡其手提包与细框眼镜的夏日休闲搭配。",
        "img": "wf-o006.png",
        "dims": {
            "outfitStyle": ["极简", "休闲"],
            "category": ["裤子", "配饰", "包", "鞋"],
            "color": ["米白", "黑", "卡其"],
            "material": [],
            "fit": ["长款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o006-trousers", "name": "米白长裤", "category": "裤子", "color": "米白", "image": _img("wf-o006-trousers.png")},
            {"id": "wf-o006-glasses", "name": "细框眼镜", "category": "配饰", "color": "棕", "image": _img("wf-o006-glasses.png")},
            {"id": "wf-o006-bag", "name": "卡其手提包", "category": "包", "color": "卡其", "image": _img("wf-o006-bag.png")},
            {"id": "wf-o006-sandals", "name": "黑色交叉带凉拖", "category": "鞋", "color": "黑", "image": _img("wf-o006-sandals.png")},
        ],
    },
    {
        "code": "WF-O007",
        "name": "米白短裤腕表搭配",
        "desc": "米白抽绳短裤与银色电子腕表组成的两件搭配，呈现简洁、轻松的日常风格。",
        "img": "wf-o007.png",
        "dims": {
            "outfitStyle": ["极简", "休闲"],
            "category": ["裤子", "配饰"],
            "color": ["米白", "灰"],
            "material": [],
            "fit": ["短款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o007-shorts", "name": "米白抽绳短裤", "category": "裤子", "color": "米白", "image": _img("wf-o007-shorts.png")},
            {"id": "wf-o007-watch", "name": "银色电子腕表", "category": "配饰", "color": "灰", "image": _img("wf-o007-watch.png")},
        ],
    },
    {
        "code": "WF-O008",
        "name": "米白长裤手提包搭配",
        "desc": "米白长裤、卡其手提包与细框眼镜组成的三件搭配，以自然中性色呈现简洁通勤风格。",
        "img": "wf-o008.png",
        "dims": {
            "outfitStyle": ["极简", "通勤", "休闲"],
            "category": ["裤子", "配饰", "包"],
            "color": ["米白", "卡其", "棕"],
            "material": [],
            "fit": ["长款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o008-trousers", "name": "米白长裤", "category": "裤子", "color": "米白", "image": _img("wf-o008-trousers.png")},
            {"id": "wf-o008-glasses", "name": "细框眼镜", "category": "配饰", "color": "棕", "image": _img("wf-o008-glasses.png")},
            {"id": "wf-o008-bag", "name": "卡其手提包", "category": "包", "color": "卡其", "image": _img("wf-o008-bag.png")},
        ],
    },
    {
        "code": "WF-O009",
        "name": "浅米短裤假日搭配",
        "desc": "浅米短裤、橄榄色太阳镜与橙色花卉书册组成的假日搭配；书册作为搭配道具保留。",
        "img": "wf-o009.png",
        "dims": {
            "outfitStyle": ["休闲"],
            "category": ["裤子", "配饰"],
            "color": ["米", "军绿", "橙"],
            "material": [],
            "fit": ["短款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o009-shorts", "name": "浅米短裤", "category": "裤子", "color": "米", "image": _img("wf-o009-shorts.png")},
            {"id": "wf-o009-sunglasses", "name": "橄榄色太阳镜", "category": "配饰", "color": "军绿", "image": _img("wf-o009-sunglasses.png")},
            {"id": "wf-o009-book", "name": "橙色花卉书册", "category": "配饰", "color": "橙", "image": _img("wf-o009-book.png")},
        ],
    },
    {
        "code": "WF-O010",
        "name": "米白长裤棕鞋搭配",
        "desc": "米白长裤、棕色皮鞋、卡其手提包与细框眼镜组成的四件搭配，以米棕配色呈现简洁通勤风格。",
        "img": "wf-o010.png",
        "dims": {
            "outfitStyle": ["极简", "通勤", "休闲"],
            "category": ["裤子", "配饰", "包", "鞋"],
            "color": ["米白", "卡其", "棕"],
            "material": [],
            "fit": ["长款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o010-trousers", "name": "米白长裤", "category": "裤子", "color": "米白", "image": _img("wf-o010-trousers.png")},
            {"id": "wf-o010-glasses", "name": "细框眼镜", "category": "配饰", "color": "棕", "image": _img("wf-o010-glasses.png")},
            {"id": "wf-o010-bag", "name": "卡其手提包", "category": "包", "color": "卡其", "image": _img("wf-o010-bag.png")},
            {"id": "wf-o010-shoes", "name": "棕色皮鞋", "category": "鞋", "color": "棕", "image": _img("wf-o010-shoes.png")},
        ],
    },
    {
        "code": "WF-O011",
        "name": "浅米双扣短裤",
        "desc": "浅米色双扣短裤，带腰袢与斜插口袋，呈现简洁休闲风格。原图仅含这一件单品，完整保留并直接用于单品查看。",
        "img": "wf-o011.png",
        "dims": {
            "outfitStyle": ["极简", "休闲"],
            "category": ["裤子"],
            "color": ["米"],
            "material": [],
            "fit": ["短款"],
            "func": [],
        },
        "items": [
            {"id": "wf-o011-shorts", "name": "浅米双扣短裤", "category": "裤子", "color": "米", "image": _img("wf-o011.png")},
        ],
    },
]
