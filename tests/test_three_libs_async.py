# -*- coding: utf-8 -*-
"""三库异步功能测试:Part A TaskStore(DB 版)单元测试 + Part B 三库 HTTP 全链路。
运行:./venv/bin/python tests/test_three_libs_async.py
"""
import sys, time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/Users/lhz/Downloads/wellflow-saas-backend-new")

from sqlalchemy import select, update, delete
from wellflow.app.database import session_scope
from wellflow.app.utils.async_task_lib import TaskStore, _ASYNC_TASK

passed, failed = [], []
def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("✅" if cond else "❌"), name, extra)

STORE = TaskStore(row_id_field="outfit_id")
_TEST_PREFIX = f"tst_{int(time.time()*1000)}_"

def _tid(name):
    return f"{_TEST_PREFIX}{name}"

# ================= Part A:TaskStore(DB 版)单元测试 =================
print("\n===== Part A:TaskStore DB 单元测试 =====")

t1 = _tid("a1")
STORE.save({"task_id": t1, "task_type": "extract", "status": "processing",
            "step": "queued", "error": "", "outfit_id": None})
got = STORE.load(t1)
check("A1 save/load 往返", got is not None and got["task_id"] == t1
      and got["status"] == "processing" and got["step"] == "queued", f"got={got}")

STORE.save({"task_id": t1, "task_type": "extract", "status": "done",
            "step": "saving", "error": "", "outfit_id": None})
with session_scope() as db:
    cnt = db.execute(select(_ASYNC_TASK).where(_ASYNC_TASK.c.task_id == t1)).all()
got = STORE.load(t1)
check("A2 upsert 更新且不重复", len(cnt) == 1 and got["status"] == "done", f"rows={len(cnt)}")

with session_scope() as db:
    old = db.execute(select(_ASYNC_TASK.c.heartbeat_at)
                     .where(_ASYNC_TASK.c.task_id == t1)).scalar()
STORE.heartbeat(t1)
with session_scope() as db:
    new = db.execute(select(_ASYNC_TASK.c.heartbeat_at)
                     .where(_ASYNC_TASK.c.task_id == t1)).scalar()
check("A3 heartbeat 刷新", new is not None and new >= old)

t4 = _tid("a4")
marked = []
store4 = TaskStore(row_id_field="outfit_id", mark_row_failed=lambda rid: marked.append(rid))
store4.save({"task_id": t4, "task_type": "generate", "status": "processing",
             "step": "cutout", "error": "", "outfit_id": 777})
with session_scope() as db:
    db.execute(update(_ASYNC_TASK)
               .where(_ASYNC_TASK.c.task_id == t4)
               .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    db.commit()
store4.sweep()
got = store4.load(t4)
check("A4 心跳超时判死", got["status"] == "failed" and "中断" in (got.get("error") or ""),
      f"status={got['status']} err={got.get('error')}")
check("A4b 判死联动业务行", 777 in marked, f"marked={marked}")

t5 = _tid("a5")
store4.save({"task_id": t5, "task_type": "extract", "status": "processing",
             "step": "cutout", "error": "", "outfit_id": 778})
store4.sweep()
got = store4.load(t5)
check("A5 心跳新鲜不被判死", got["status"] == "processing", f"status={got['status']}")

t6 = _tid("a6")
store4.save({"task_id": t6, "task_type": "extract", "status": "failed", "error": "x"})
with session_scope() as db:
    db.execute(update(_ASYNC_TASK)
               .where(_ASYNC_TASK.c.task_id == t6)
               .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    db.commit()
store4.sweep()
got = store4.load(t6)
check("A6 TTL 过期删除", got is None, f"got={got}")

t7 = _tid("a7")
store4.save({"task_id": t7, "task_type": "auto_tag", "status": "done", "step": "saving",
             "error": "", "tags": [{"group_key": "基础身份"}], "description": "测试描述"})
got = store4.load(t7)
check("A7 payload 结果往返", got and got.get("description") == "测试描述"
      and got.get("tags") == [{"group_key": "基础身份"}])

with session_scope() as db:
    for tid in [t1, t4, t5, t6, t7]:
        db.execute(delete(_ASYNC_TASK).where(_ASYNC_TASK.c.task_id == tid))
    db.commit()
print("(测试数据已清理)")

# ================= Part B:三库 HTTP 全链路 =================
print("\n===== Part B:三库 HTTP 全链路 =====")
import requests
BASE = "http://127.0.0.1:8000"

r = requests.post(f"{BASE}/api/outfit/ai-extract", json={
    "original_uri": "/static/assets/outfit-demo/original.png",
    "session_id": f"tst_http_{int(time.time()*1000)}", "mode": "demo"}, timeout=15)
d = r.json().get("data", {})
check("B1 穿搭拆解秒回", r.status_code == 200 and d.get("status") == "extracting"
      and d.get("outfit_id"), f"resp={d}")
oid = d.get("outfit_id"); tid = d.get("task_id")
ostatus = ""
if tid:
    deadline = time.time() + 120
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/outfit/ai-status", params={"task_id": tid}, timeout=15)
        ostatus = r.json().get("data", {}).get("status")
        if ostatus in ("done", "failed"):
            break
        time.sleep(3)
check("B2 穿搭轮询 done", ostatus == "done", f"status={ostatus}")
r = requests.get(f"{BASE}/api/outfit/{oid}", timeout=15)
check("B3 穿搭详情待选件", r.json().get("data", {}).get("status") == "pending_select"
      and len(r.json().get("data", {}).get("items", [])) == 6)

r = requests.get(f"{BASE}/api/scene", params={"page_size": 1}, timeout=15)
check("B4 场景列表", r.status_code == 200 and "items" in r.json().get("data", {}))

# ── 场景全链路(真 AI:提取 + 马赛克 + 打标,约 1~5 分钟)→ 确认入库即 active ──
r = requests.post(f"{BASE}/api/scene/ai-extract", json={
    "original_uri": "/static/assets/outfit-demo/original.png",
    "session_id": f"tst_scene_{int(time.time()*1000)}"}, timeout=15)
d = r.json().get("data", {})
sid2, stid = d.get("scene_id"), d.get("task_id")
check("B4b 场景处理秒回", r.status_code == 200 and d.get("status") == "extracting"
      and sid2 and stid, f"resp={d}")
status = ""
if sid2 and stid:
    deadline = time.time() + 360
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/scene/ai-status",
                         params={"task_id": stid}, timeout=15)
        status = r.json().get("data", {}).get("status")
        if status in ("done", "failed"):
            break
        time.sleep(5)
    r = requests.get(f"{BASE}/api/scene/{sid2}", timeout=15)
    det = r.json().get("data", {})
    check("B4c 场景轮询 done+马赛克原图", status == "done" and "mosaic_url" in det,
          f"status={status} mosaic={det.get('mosaic_url')}")
    r = requests.put(f"{BASE}/api/scene/{sid2}", json={"status": "active"}, timeout=15)
    det = r.json().get("data", {})
    check("B4d 确认入库即 active(无审核)", r.status_code == 200
          and det.get("status") == "active", f"resp={det.get('status')}")
    r = requests.put(f"{BASE}/api/scene/{sid2}", json={"status": "active"}, timeout=15)
    check("B4e 非待确认行不可确认(400)", r.status_code == 400, f"code={r.status_code}")
    requests.delete(f"{BASE}/api/scene/{sid2}", timeout=15)

r = requests.get(f"{BASE}/api/reference/mannequins/dimensions", timeout=15)
check("B5 模特维度", r.status_code == 200 and "groups" in r.json().get("data", {}))
r = requests.get(f"{BASE}/api/reference/mannequins", timeout=15)
check("B6 模特列表", r.status_code == 200 and "items" in r.json().get("data", {}))

r = requests.post(f"{BASE}/api/reference/mannequins/async-generate",
                  data={"name": "tst"},
                  files={"cover_image": ("t.png",
                          open("/Users/lhz/Downloads/wellflow-saas-backend-new/static/assets/outfit-demo/original.png", "rb"),
                          "image/png")}, timeout=15)
d = r.json().get("data", {})
check("B7 模特一键入库秒回", r.status_code == 200 and d.get("status") == "generating"
      and d.get("mannequin_id") and d.get("task_id"), f"resp={d}")
if d.get("mannequin_id"):
    requests.delete(f"{BASE}/api/reference/mannequins/{d['mannequin_id']}", timeout=15)

print(f"\n通过 {len(passed)} 项 / 失败 {len(failed)} 项")
sys.exit(1 if failed else 0)
