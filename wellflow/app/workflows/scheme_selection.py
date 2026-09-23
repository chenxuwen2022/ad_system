"""Resolve user-visible scheme numbers before choosing model input."""
from __future__ import annotations

import re
from typing import Any


def explicit_scheme_indices(instruction: str) -> list[int] | None:
    """Recognize explicit scheme numbers; internal indices are zero-based."""
    pattern = r"方案\s*([0-9一二三四五六七八九十]+)|第\s*([0-9一二三四五六七八九十]+)\s*(?:套(?:方案)?|个方案)"
    numbers = [left or right for left, right in re.findall(pattern, instruction)]
    if not numbers:
        return None
    indices = []
    digits = {c: i for i, c in enumerate('零一二三四五六七八九')}
    for number in numbers:
        if number.isascii() and number.isdigit():
            value = int(number)
        elif '十' in number:
            left, right = number.split('十', 1)
            if len(left) > 1 or len(right) > 1:
                raise ValueError('无法识别方案编号')
            value = (digits.get(left, 1) * 10) + digits.get(right, 0)
        else:
            value = digits.get(number, 0)
        if value - 1 not in indices:
            indices.append(value - 1)
    return indices


def validate_scheme_indices(selection: Any, count: int) -> list[int]:
    if selection == 'all':
        return list(range(count))
    if not isinstance(selection, list) or not selection:
        raise ValueError('请明确指定要使用的商拍方案')
    result = []
    for value in selection:
        if type(value) is int:
            index = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            index = int(value)
        else:
            raise ValueError('商拍方案索引无效')
        if not 0 <= index < count:
            raise ValueError(f'方案{index + 1}不存在，请重新选择')
        if index not in result:
            result.append(index)
    return result


def load_initial_schemes(task_id: str) -> list[dict[str, Any]]:
    """Recover the first published C2 result for tasks created before snapshots existed."""
    if not task_id:
        return []
    from sqlalchemy import select
    from wellflow.app.database import session_scope
    from wellflow.app.models.task_models import TaskEvent
    with session_scope() as db:
        event = db.scalar(select(TaskEvent).where(
            TaskEvent.task_id == task_id, TaskEvent.event_type == 'graph_interrupt_c2',
        ).order_by(TaskEvent.event_id).limit(1))
        return (event.payload_json.get('schemes') or []) if event else []
