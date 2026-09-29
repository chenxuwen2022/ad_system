"""Tenant-scoped account mutations and operation logs share a transaction."""
from fastapi import HTTPException
from sqlalchemy import Integer, and_, func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wellflow.app.api.utils import to_cn_iso
from wellflow.app.models.account_models import Company, OperationLog, User
from wellflow.app.security import PLATFORM_ADMIN, hash_password

ROLE_LABELS = {
    "platform_admin": "平台超管",
    "company_admin": "公司管理员",
    "company_user": "普通员工",
}


def mask_phone(phone: str | None) -> str:
    """对电话号进行脱敏。规则：长度 ≥ 11 → 前 3 + **** + 后 4；
    长度 8~10 → 前 2 + **** + 后 2；长度 ≤ 7 → 原样返回（无法安全遮）。空值返回空串。"""
    if not phone:
        return ''
    n = len(phone)
    if n >= 11:
        return f"{phone[:3]}****{phone[-4:]}"
    if n >= 8:
        return f"{phone[:2]}****{phone[-2:]}"
    return phone


def user_data(db: Session, user: User) -> dict:
    company = db.get(Company, user.company_id)
    return {
        "id": user.id, "company_id": user.company_id, "company_name": company.name,
        "username": user.username, "email": user.email, "phone": user.phone,
        "role": user.role, "created_at": to_cn_iso(user.created_at), "updated_at": to_cn_iso(user.updated_at),
    }


def user_scope(actor: User):
    conditions = [User.deleted_at.is_(None)]
    if actor.role == PLATFORM_ADMIN:
        # platform_admin 可以看到全部用户，包括其他 platform_admin
        pass
    else:
        conditions.append(User.role != PLATFORM_ADMIN)
        conditions.append(User.company_id == actor.company_id)
    return conditions


def lock_account_changes(db: Session):
    """Serialize admin removals across companies before acquiring company/user locks."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(74120318)))


def get_target(db: Session, actor: User, user_id: int, *, lock=False) -> User:
    if lock:
        lock_account_changes(db)
    query = select(User).where(User.id == user_id, *user_scope(actor))
    target = db.scalar(query.execution_options(populate_existing=True))
    if target is None:
        raise HTTPException(404, "用户不存在")
    if lock:
        # Always lock company first. Two simultaneous administrator removals
        # must serialize before counting administrators or locking target rows.
        db.scalar(select(Company).where(Company.id == target.company_id).with_for_update())
        # Recheck the actor after waiting: another admin may have just demoted
        # or deleted them. Do not authorize a queued write with stale identity.
        current = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
        if current is None or current.deleted_at is not None or current.role not in {PLATFORM_ADMIN, "company_admin"}:
            raise HTTPException(403, "管理权限已变更，请刷新页面")
        target = db.scalar(select(User).where(User.id == user_id, *user_scope(current))
                           .with_for_update().execution_options(populate_existing=True))
        if target is None:
            raise HTTPException(404, "用户不存在")
    return target


def ensure_other_admin(db: Session, target: User):
    if target.role == "company_admin":
        count = db.scalar(select(func.count()).select_from(User).where(
            User.company_id == target.company_id, User.role == "company_admin",
            User.deleted_at.is_(None), User.id != target.id))
        if count == 0:
            raise HTTPException(409, "每家公司至少需要保留一名公司管理员")
    if target.role == PLATFORM_ADMIN:
        count = db.scalar(select(func.count()).select_from(User).where(
            User.role == PLATFORM_ADMIN, User.deleted_at.is_(None), User.id != target.id))
        if count == 0:
            raise HTTPException(409, "系统至少需要保留一名平台超管")


def operator_role_snapshot(actor: User) -> str:
    # Self-demotion must still be recorded with the authority used for the action.
    previous = inspect(actor).attrs.role.history.deleted
    return previous[0] if previous else actor.role


def record_operation(db: Session, actor: User, target: User, action: str, detail: str):
    db.add(OperationLog(
        company_id=target.company_id, operator_id=actor.id,
        operator_username=actor.username, operator_role=operator_role_snapshot(actor), target_user_id=target.id,
        target_username=target.username, action=action, detail=detail,
    ))


def commit_account_change(db: Session):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "邮箱已被使用，请检查后重试") from exc


def insert_user(db: Session, *, company_id: int, username: str, email: str, password: str,
                phone: str | None = None, role: str = "company_user") -> User:
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(409, "该邮箱已被使用")
    user = User(company_id=company_id, email=email, username=username, phone=phone,
                password_hash=hash_password(password), role=role)
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "邮箱已被使用，请检查后重试") from exc
    return user


def sync_company_email_suffixes(db: Session, company: Company) -> int:
    """Caller holds the company lock; all active account updates share its transaction.

    Keep login usernames stable: this operation changes only the email domain.
    """
    users = list(db.scalars(select(User).where(
        User.company_id == company.id, User.deleted_at.is_(None)
    ).order_by(User.id).with_for_update().execution_options(populate_existing=True)))
    updates = []
    destinations = set()
    for user in users:
        prefix, _, domain = user.email.rpartition("@")
        if "@" + domain.lower() in company.email_suffixes:
            continue
        email = prefix + company.email_suffixes[0]
        if len(email) > 254:
            raise HTTPException(409, f"用户 {user.username} 的新邮箱过长，整次修改未保存")
        if email in destinations or db.scalar(select(User.id).where(User.email == email, User.id != user.id)) is not None:
            raise HTTPException(409, f"用户 {user.username} 的新邮箱 {email} 已被使用，整次修改未保存")
        destinations.add(email)
        updates.append((user, email))
    for user, email in updates:
        user.email = email
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "新邮箱发生冲突，整次修改未保存，请刷新后核对") from exc
    return len(updates)


def change_user_email(db: Session, target: User, email: str, *, company_id: int | None = None, username: str | None = None):
    """Keep email unique when editing a user or transferring companies."""
    destination_id = company_id if company_id is not None else target.company_id
    username = username if username is not None else target.username
    if db.scalar(select(User.id).where(User.id != target.id, User.email == email)) is not None:
        raise HTTPException(409, "该邮箱已被使用")
    try:
        with db.begin_nested():
            target.email = email
            target.username = username
            target.company_id = destination_id
            db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "邮箱已被使用，请检查后重试") from exc


def lock_batch_targets(db: Session, actor: User, user_ids: list[int]) -> list[User]:
    lock_account_changes(db)
    targets = list(db.scalars(select(User).where(*user_scope(actor), User.id.in_(user_ids))))
    if len(targets) != len(user_ids):
        raise HTTPException(404, "包含不存在或无权操作的用户，整批未修改")
    for company_id in sorted({target.company_id for target in targets}):
        db.scalar(select(Company).where(Company.id == company_id).with_for_update())
    actor = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
    if actor is None or actor.deleted_at is not None or actor.role not in {PLATFORM_ADMIN, "company_admin"}:
        raise HTTPException(403, "管理权限已变更，请刷新页面")
    targets = list(db.scalars(select(User).where(*user_scope(actor), User.id.in_(user_ids))
                             .order_by(User.id).with_for_update().execution_options(populate_existing=True)))
    if len(targets) != len(user_ids):
        raise HTTPException(404, "用户状态已变更或无权操作，整批未修改")
    return targets


def ensure_batch_admins(db: Session, targets: list[User], *, allow_empty_companies: bool = False):
    ids = [target.id for target in targets]
    for company_id in {target.company_id for target in targets
                       if target.role == "company_admin"}:
        remaining = db.scalar(select(func.count()).select_from(User).where(
            User.company_id == company_id, User.role == "company_admin", User.deleted_at.is_(None),
            User.id.not_in(ids)))
        if not remaining:
            # Only deletion by a platform admin may leave a company completely empty.
            remaining_users = db.scalar(select(func.count()).select_from(User).where(
                User.company_id == company_id, User.deleted_at.is_(None), User.id.not_in(ids)
            )) if allow_empty_companies else None
            if allow_empty_companies and remaining_users == 0:
                continue
            raise HTTPException(409, "删除或调整后公司仍有用户，必须保留至少一名公司管理员。请取消选择一位管理员，或先任命其他管理员。整批未修改")
    if any(target.role == PLATFORM_ADMIN for target in targets):
        remaining = db.scalar(select(func.count()).select_from(User).where(
            User.role == PLATFORM_ADMIN, User.deleted_at.is_(None), User.id.not_in(ids)))
        if not remaining:
            raise HTTPException(409, "系统至少保留一名平台超管，整批未修改")


def record_batch_operation(db: Session, actor: User, targets: list[User], action: str,
                           detail: str, details: dict[int, str] | None = None):
    operator_role = operator_role_snapshot(actor)
    snapshots = []
    for user in targets:
        snapshot = {"id": user.id, "username": user.username, "company_id": user.company_id,
                    "company_name": db.get(Company, user.company_id).name}
        target_detail = details[user.id] if details else detail
        # Common text is stored once on the log; retain only per-user differences.
        if target_detail != detail:
            snapshot["detail"] = target_detail
        snapshots.append(snapshot)
    # company_id is a legacy anchor only. Visibility uses the scoped snapshots below.
    db.add(OperationLog(company_id=targets[0].company_id, operator_id=actor.id, operator_role=operator_role,
                        operator_username=actor.username, target_user_id=None,
                        target_username="批量操作", action=action, detail=detail, targets=snapshots))


def operation_log_scope(db: Session, company_id: int | None, target: str):
    # Filter within the same target entry to avoid leaking other tenants through searches.
    if db.bind.dialect.name == "postgresql":
        entries = func.json_array_elements(OperationLog.targets).table_valued("value").alias("log_target")
        target_company = entries.c.value.op("->>")("company_id").cast(Integer)
        target_name = entries.c.value.op("->>")("username")
    else:
        entries = func.json_each(OperationLog.targets).table_valued("value").alias("log_target")
        target_company = func.json_extract(entries.c.value, "$.company_id")
        target_name = func.json_extract(entries.c.value, "$.username")
    legacy = [func.json_array_length(OperationLog.targets) == 0]
    matching = []
    if company_id is not None:
        legacy.append(OperationLog.company_id == company_id)
        matching.append(target_company == company_id)
    if target:
        legacy.append(OperationLog.target_username.contains(target, autoescape=True))
        matching.append(target_name.contains(target, autoescape=True))
    batch = select(1).select_from(entries).where(*matching).correlate(OperationLog).exists()
    return or_(and_(*legacy), batch)


def operation_log_data(log: OperationLog, company_name: str, company_id: int | None):
    targets = [{**entry, "detail": entry.get("detail", log.detail)}
               for entry in log.targets if company_id is None or entry["company_id"] == company_id]
    names = list(dict.fromkeys(entry["company_name"] for entry in targets))
    ids = set(entry["company_id"] for entry in targets)
    return {"id": log.id, "company_id": (next(iter(ids)) if len(ids) == 1 else None) if targets else log.company_id,
            "company_name": "、".join(names) if targets else company_name,
            "operator_id": log.operator_id, "operator_username": log.operator_username,
            "target_user_id": log.target_user_id,
            "target_username": ("、".join(entry["username"] for entry in targets[:3]) + ("等" if len(targets) > 3 else "")) if targets else log.target_username,
            "action": log.action, "detail": f"{log.detail}（共 {len(targets)} 人）" if targets else log.detail,
            "targets": targets, "created_at": to_cn_iso(log.created_at)}
