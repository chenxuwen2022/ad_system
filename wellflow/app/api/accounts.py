"""Minimal login, company user management and administrator operation logs."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import func, select, or_, delete
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from wellflow.app.config import settings
from wellflow.app.database import get_db
from wellflow.app.models.account_models import Company, OperationLog, User, utcnow
from wellflow.app.schemas.account_schemas import CompanyCreate, CompanyUpdate, BulkUserIds, BulkUserDelete, CompanySuffixInput, normalize_email, BulkRoleUpdate, BulkUserCreate, ChangeOwnPassword, UpdateOwnPhone, LoginInput, PasswordReset, RoleUpdate, UserCreate, UserUpdate
from wellflow.app.security import (
    PLATFORM_ADMIN, dummy_password_hash, get_current_user, hash_password,
    issue_token, login_limiter, require_platform_admin, require_admin, verify_password,
)
from wellflow.app.services.accounts import (
    lock_account_changes, lock_batch_targets, ensure_batch_admins, record_batch_operation, operation_log_scope, operation_log_data,
    sync_company_email_suffixes, operator_role_snapshot, ROLE_LABELS, commit_account_change, ensure_other_admin, get_target,
    insert_user, change_user_email, record_operation, user_data, user_scope, mask_phone,
)
from wellflow.app.api.utils import ok, page, to_cn_iso

class AccountRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default response includes the rejected input, which
                # must never echo passwords from login/reset requests.
                details = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                           for error in exc.errors()]
                raise HTTPException(422, details) from exc
        return safe_handler


router = APIRouter(tags=["账号与用户管理"], route_class=AccountRoute)


@router.post("/auth/login")
def login(body: LoginInput, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email
    login_limiter.check(request.client.host if request.client else "unknown", email)
    user = db.scalar(select(User).where(User.email == email, User.deleted_at.is_(None)))
    valid = verify_password(body.password, user.password_hash if user else dummy_password_hash())
    if user is None or not valid:
        raise HTTPException(401, "邮箱或密码错误")
    token = issue_token(user)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie("wellflow_media", token, httponly=True, secure=settings.auth_cookie_secure,
                        samesite="strict", path="/api/uploaded_media", max_age=settings.auth_token_hours * 3600)
    return ok({"access_token": token, "token_type": "bearer", "expires_in": settings.auth_token_hours * 3600,
               "user": user_data(db, user)})


@router.post("/auth/logout")
def logout(response: Response, user: User = Depends(get_current_user)):
    # This clears only the media cookie; there is deliberately no session table.
    response.delete_cookie("wellflow_media", path="/api/uploaded_media", secure=settings.auth_cookie_secure,
                           httponly=True, samesite="strict")
    return ok()


@router.get("/me")
def me(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return ok(user_data(db, user))


@router.post("/me/change-password")
def change_own_password(body: ChangeOwnPassword, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Serialize concurrent resets and re-check the password against the latest row.
    user = db.scalar(select(User).where(User.id == actor.id).with_for_update()
                     .execution_options(populate_existing=True))
    if user is None or user.deleted_at is not None:
        raise HTTPException(401, "账号已删除，请联系管理员")
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "当前密码不正确")
    user.password_hash = hash_password(body.new_password)
    if user.role in {PLATFORM_ADMIN, "company_admin"}:
        record_operation(db, user, user, "user.reset_password", "修改了本人的密码")
    commit_account_change(db)
    return ok(message="密码已修改，下次登录请使用新密码")


@router.patch("/me/phone")
def update_own_phone(body: UpdateOwnPhone, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
    if user is None or user.deleted_at is not None:
        raise HTTPException(401, "账号已删除，请联系管理员")
    old = user.phone or '未填写'
    new = body.phone or '未填写'
    if old == new:
        return ok(message="电话号码未改变")
    user.phone = body.phone
    if user.role in {PLATFORM_ADMIN, "company_admin"}:
        record_operation(db, user, user, "user.update", f"修改了本人的电话：{mask_phone(old) if old != '未填写' else old} → {mask_phone(new) if new != '未填写' else new}")
    commit_account_change(db)
    return ok(user_data(db, user))


@router.get("/companies")
def companies(
    page: int = Query(1, ge=1, alias="page"),
    page_size: int = Query(50, ge=1, le=100, alias="page_size"),
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List active companies visible to the current administrator.

    目前租户规模下公司数量有限，暂按全量返回处理；page/page_size 预留以对齐前端统一的分页契约，
    后续公司数膨胀到需要分页时，在这里补上 offset/limit 即可。
    """
    _ = (page, page_size)  # 预留参数，暂不分页
    query = select(Company).where(Company.deleted_at.is_(None)).order_by(Company.id)
    if actor.role != PLATFORM_ADMIN:
        query = query.where(Company.id == actor.company_id)
    return ok([company_data(company) for company in db.scalars(query)])


@router.get("/companies/page")
def company_page(
    page_number: int = Query(1, ge=1, alias="page"),
    page_size: int = Query(50, ge=1, le=100),
    search: str = Query("", max_length=254),
    company_id: int | None = Query(None, ge=1),
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    conditions = [Company.deleted_at.is_(None)]
    if actor.role != PLATFORM_ADMIN:
        conditions.append(Company.id == actor.company_id)
    if company_id is not None:
        conditions.append(Company.id == company_id)
    if keyword := search.strip().lower():
        # Search individual suffix values, not JSON serialization punctuation.
        entries = (func.json_array_elements_text(Company.email_suffixes)
                   if db.bind.dialect.name == "postgresql"
                   else func.json_each(Company.email_suffixes)).table_valued("value").alias("suffix")
        suffix_match = select(1).select_from(entries).where(
            func.lower(entries.c.value).contains(keyword, autoescape=True)
        ).correlate(Company).exists()
        conditions.append(or_(func.lower(Company.name).contains(keyword, autoescape=True), suffix_match))
    total = db.scalar(select(func.count()).select_from(Company).where(*conditions)) or 0
    rows = db.scalars(select(Company).where(*conditions).order_by(Company.id)
                      .offset((page_number - 1) * page_size).limit(page_size))
    return ok({"items": [company_data(company) for company in rows],
               "total": total, "page": page_number, "page_size": page_size})


def company_data(company: Company):
    return {
        "id": company.id,
        "name": company.name,
        "email_suffixes": company.email_suffixes,
        "allow_platform_admin": company.allow_platform_admin,
    }


def lock_company(db: Session, actor: User, company_id: int):
    if actor.role != PLATFORM_ADMIN and actor.company_id != company_id:
        raise HTTPException(403, "不能管理其他公司的邮箱后缀")
    company = db.scalar(select(Company).where(Company.id == company_id, Company.deleted_at.is_(None)).with_for_update()
                        .execution_options(populate_existing=True))
    if company is None:
        raise HTTPException(404, "公司不存在")
    actor = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
    if actor is None or actor.deleted_at is not None or actor.role not in {PLATFORM_ADMIN, "company_admin"}:
        raise HTTPException(403, "管理权限已变更，请刷新页面")
    if actor.role != PLATFORM_ADMIN and actor.company_id != company_id:
        raise HTTPException(403, "不能管理其他公司的邮箱后缀")
    return company


def log_suffix_change(db: Session, actor: User, company: Company, action: str, detail: str):
    db.add(OperationLog(company_id=company.id, operator_id=actor.id, operator_username=actor.username, operator_role=operator_role_snapshot(actor),
                        target_user_id=None, target_username=company.name, action=action, detail=detail))


@router.post("/companies/{company_id}/email-suffixes", status_code=201)
def add_company_suffix(company_id: int, body: CompanySuffixInput, actor: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    company = lock_company(db, actor, company_id)
    if body.suffix in company.email_suffixes:
        raise HTTPException(409, "该邮箱后缀已存在")
    company.email_suffixes = [*company.email_suffixes, body.suffix]
    log_suffix_change(db, actor, company, "company.add_email_suffix", f"为公司 {company.name} 新增邮箱后缀 {body.suffix}")
    commit_account_change(db)
    return ok(company_data(company))


@router.delete("/companies/{company_id}/email-suffixes")
def delete_company_suffix(company_id: int, body: CompanySuffixInput, actor: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    company = lock_company(db, actor, company_id)
    if body.suffix not in company.email_suffixes:
        raise HTTPException(404, "该邮箱后缀已删除，请刷新公司列表")
    if len(company.email_suffixes) <= 1:
        raise HTTPException(409, "每家公司至少保留一个邮箱后缀")
    company.email_suffixes = [suffix for suffix in company.email_suffixes if suffix != body.suffix]
    count = sync_company_email_suffixes(db, company)
    log_suffix_change(db, actor, company, "company.delete_email_suffix",
                      f"从公司 {company.name} 删除邮箱后缀 {body.suffix}，同步 {count} 位用户邮箱为 {company.email_suffixes[0]} 后缀")
    commit_account_change(db)
    return ok(company_data(company))


def commit_company_change(db: Session):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "公司名称已存在，请换一个名称") from exc


def check_company_name(db: Session, name: str, exclude: int | None = None):
    query = select(Company.id).where(Company.deleted_at.is_(None), func.lower(func.trim(Company.name)) == name.lower())
    if exclude is not None:
        query = query.where(Company.id != exclude)
    if db.scalar(query) is not None:
        raise HTTPException(409, "公司名称已存在，请换一个名称")


def platform_company(db: Session, actor: User, company_id: int):
    company = lock_company(db, actor, company_id)
    require_platform_admin(actor)
    return company


@router.post("/companies", status_code=201)
def create_company(body: CompanyCreate, actor: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    check_company_name(db, body.name)
    company = Company(name=body.name, email_suffixes=body.email_suffixes)
    db.add(company)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "公司名称已存在，请换一个名称") from exc
    log_suffix_change(db, actor, company, "company.create", f"新增公司 {company.name}")
    commit_company_change(db)
    return ok(company_data(company))


@router.patch("/companies/{company_id}")
def update_company(company_id: int, body: CompanyUpdate, actor: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    company = platform_company(db, actor, company_id)
    check_company_name(db, body.name, company_id)
    if not (body.email_suffixes if body.email_suffixes is not None else company.email_suffixes):
        raise HTTPException(422, "每家公司至少保留一个邮箱后缀")
    changes = []
    if company.name != body.name:
        changes.append(f"公司名称从 {company.name} 修改为 {body.name}")
        company.name = body.name
    if body.email_suffixes is not None:
        added = [suffix for suffix in body.email_suffixes if suffix not in company.email_suffixes]
        removed = [suffix for suffix in company.email_suffixes if suffix not in body.email_suffixes]
        if added:
            changes.append("新增邮箱后缀：" + "、".join(added))
        if removed:
            changes.append("删除邮箱后缀：" + "、".join(removed))
        if added or removed:
            company.email_suffixes = body.email_suffixes
        if removed:
            count = sync_company_email_suffixes(db, company)
            changes.append(f"同步 {count} 位用户邮箱为 {company.email_suffixes[0]} 后缀")
    if changes:
        log_suffix_change(db, actor, company, "company.update", "；".join(changes))
        commit_company_change(db)
    return ok(company_data(company))


@router.delete("/companies/{company_id}")
def delete_company(company_id: int, actor: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    company = platform_company(db, actor, company_id)
    if db.scalar(select(User.id).where(User.company_id == company_id, User.deleted_at.is_(None)).limit(1)) is not None:
        raise HTTPException(409, "公司仍有在用员工，请先删除员工后再删除公司")
    db.execute(delete(User).where(User.company_id == company_id, User.deleted_at.is_not(None)))
    company.deleted_at = utcnow()
    log_suffix_change(db, actor, company, "company.delete", f"删除公司 {company.name}，历史操作日志保留")
    commit_company_change(db)
    return ok()


@router.get("/users")
def list_users(page_num: int = Query(1, alias="page", ge=1), page_size: int = Query(50, ge=1, le=100),
               search: str = Query("", max_length=254),
               role: Literal["platform_admin", "company_admin", "company_user"] | None = None,
               company_id: int | None = Query(None, gt=0), actor: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    conditions = user_scope(actor)
    if company_id is not None:
        conditions.append(User.company_id == company_id)
    if role:
        conditions.append(User.role == role)
    if search.strip():
        conditions.append(User.username.contains(search.strip(), autoescape=True) | User.email.contains(search.strip(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(User).where(*conditions))
    users = db.scalars(select(User).where(*conditions).order_by(User.id.desc())
                       .offset((page_num - 1) * page_size).limit(page_size))
    return page([user_data(db, user) for user in users], total, page_num, page_size)


def validate_creation_role(actor: User, company: Company, role: str):
    if actor.role != PLATFORM_ADMIN and actor.company_id != company.id:
        raise HTTPException(403, "不能在其他公司创建用户")
    if role == PLATFORM_ADMIN:
        if actor.role != PLATFORM_ADMIN:
            raise HTTPException(403, "只有平台超管可以分配「平台超管」角色")
        if not company.allow_platform_admin:
            raise HTTPException(403, "该公司不允许设置为平台超管")


@router.post("/users/bulk", status_code=201)
def bulk_create_users(body: BulkUserCreate, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    company_id = body.company_id if actor.role == PLATFORM_ADMIN else actor.company_id
    if actor.role != PLATFORM_ADMIN and body.company_id not in (None, actor.company_id):
        raise HTTPException(403, "不能在其他公司创建用户")
    if company_id is None:
        raise HTTPException(422, "请先选择目标公司")
    company = db.scalar(select(Company).where(Company.id == company_id, Company.deleted_at.is_(None)).with_for_update())
    if company is None:
        raise HTTPException(404, "公司不存在")
    actor = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
    if actor is None or actor.deleted_at is not None or actor.role not in {PLATFORM_ADMIN, "company_admin"}:
        raise HTTPException(403, "管理权限已变更，请刷新页面")
    if actor.role != PLATFORM_ADMIN and actor.company_id != company_id:
        raise HTTPException(403, "不能在其他公司创建用户")
    if body.email_suffix not in company.email_suffixes:
        raise HTTPException(409, "该邮箱后缀不属于当前公司或已被删除，请刷新后重新选择")
    emails = body.emails
    if len(set(emails)) != len(emails):
        raise HTTPException(422, "拼接后的邮箱重复，请检查用户名")
    existing = list(db.scalars(select(User.email).where(User.email.in_(emails))))
    if existing:
        raise HTTPException(409, "以下邮箱已被使用，整批未新增：" + "、".join(existing))
    validate_creation_role(actor, company, body.role)
    initial = settings.auth_initial_password.get_secret_value()
    if not initial or len(initial.encode("utf-8")) > 72:
        raise HTTPException(503, "请联系平台管理员配置有效的初始密码")
    users = []
    for username, email in zip(body.usernames, emails):
        user = insert_user(db, company_id=company_id, username=username, email=email, password=initial, role=body.role)
        users.append(user)
    record_batch_operation(db, actor, users, "user.create", f"新增用户，角色为{ROLE_LABELS[body.role]}")
    commit_account_change(db)
    return ok([user_data(db, user) for user in users])


@router.put("/users/bulk-role")
def bulk_assign_role(body: BulkRoleUpdate, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    targets = lock_batch_targets(db, actor, body.user_ids)
    if body.role == PLATFORM_ADMIN and actor.role != PLATFORM_ADMIN:
        raise HTTPException(403, "只有平台超管可以分配「平台超管」角色")
    if body.role == PLATFORM_ADMIN:
        for target in targets:
            company = db.get(Company, target.company_id)
            if company is None or not company.allow_platform_admin:
                raise HTTPException(
                    403,
                    f"用户 {target.username} 所属公司不允许设置为平台超管",
                )
    # Only users leaving their current role are removed from its remaining count.
    ensure_batch_admins(db, [target for target in targets if target.role != body.role])
    details = {}
    for target in targets:
        old = target.role
        details[target.id] = (f"从{ROLE_LABELS[old]}调整为{ROLE_LABELS[body.role]}"
                              if old != body.role else f"保持{ROLE_LABELS[body.role]}，未变更")
        target.role = body.role
    record_batch_operation(db, actor, targets, "user.assign_role", f"配置权限为{ROLE_LABELS[body.role]}", details)
    commit_account_change(db)
    return ok([user_data(db, target) for target in targets])


def confirm_company_emptying(db: Session, targets: list[User], confirmed_company_ids: list[int]):
    user_ids = [target.id for target in targets]
    # Check all active accounts under the company locks, not just the visible page.
    emptied = []
    for company_id in sorted({target.company_id for target in targets}):
        remaining = db.scalar(select(func.count()).select_from(User).where(
            User.company_id == company_id, User.deleted_at.is_(None), User.id.not_in(user_ids)))
        if not remaining:
            emptied.append({"id": company_id, "name": db.get(Company, company_id).name,
                            "user_count": sum(target.company_id == company_id for target in targets)})
    if any(company["id"] not in confirmed_company_ids for company in emptied):
        raise HTTPException(409, {"code": "company_empty_confirmation_required", "companies": emptied})


@router.post("/users/bulk-delete")
def bulk_delete_users(body: BulkUserDelete, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if actor.id in body.user_ids:
        raise HTTPException(409, "不能删除自己，请取消勾选本人后重试")
    targets = lock_batch_targets(db, actor, body.user_ids)
    ensure_batch_admins(db, targets, allow_empty_companies=actor.role == PLATFORM_ADMIN)
    confirm_company_emptying(db, targets, body.confirmed_empty_company_ids)
    record_batch_operation(db, actor, targets, "user.delete", "删除用户")
    for target in targets:
        db.delete(target)
    commit_account_change(db)
    return ok({"deleted_count": len(targets)})


@router.get("/users/{user_id}")
def get_user(user_id: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    return ok(user_data(db, get_target(db, actor, user_id)))


@router.post("/users", status_code=201)
def create_user(body: UserCreate, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    company_id = body.company_id if actor.role == PLATFORM_ADMIN else actor.company_id
    if actor.role != PLATFORM_ADMIN and body.company_id not in (None, actor.company_id):
        raise HTTPException(403, "不能在其他公司创建用户")
    if company_id is None:
        raise HTTPException(422, "请先选择目标公司")
    company = db.scalar(select(Company).where(Company.id == company_id, Company.deleted_at.is_(None)).with_for_update())
    if company is None:
        raise HTTPException(404, "公司不存在")
    actor = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
    if actor is None or actor.deleted_at is not None or actor.role not in {PLATFORM_ADMIN, "company_admin"}:
        raise HTTPException(403, "管理权限已变更，请刷新页面")
    validate_creation_role(actor, company, body.role)
    initial = settings.auth_initial_password.get_secret_value()
    if not initial or len(initial.encode("utf-8")) > 72:
        raise HTTPException(503, "请联系平台管理员配置有效的初始密码")
    user = insert_user(db, company_id=company_id, username=body.username, email=body.email, phone=body.phone, password=initial, role=body.role)
    record_operation(db, actor, user, "user.create", f"新增用户 {user.username}，角色为{ROLE_LABELS[body.role]}")
    commit_account_change(db)
    return ok(user_data(db, user), "用户已创建，使用配置的初始密码登录")


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    lock_account_changes(db)
    initial = get_target(db, actor, user_id)
    source_id = initial.company_id
    destination_id = body.company_id if body.company_id is not None else source_id
    if actor.role != PLATFORM_ADMIN and destination_id != actor.company_id:
        raise HTTPException(403, "公司管理员只能分配本公司")
    # Stable lock order also serializes opposite-direction transfers and company deletion.
    for ident in sorted({source_id, destination_id}):
        company = db.scalar(select(Company).where(Company.id == ident, Company.deleted_at.is_(None))
                            .with_for_update().execution_options(populate_existing=True))
        if company is None:
            raise HTTPException(404, "公司不存在")
    target = get_target(db, actor, user_id)
    if target.company_id != source_id:
        raise HTTPException(409, "用户所属公司已变更，请刷新后重试")
    target = get_target(db, actor, user_id, lock=True)
    if target.company_id != source_id:
        raise HTTPException(409, "用户所属公司已变更，请刷新后重试")
    if actor.role != PLATFORM_ADMIN and destination_id != actor.company_id:
        raise HTTPException(403, "公司管理员只能分配本公司")
    changes = []
    next_role = body.role if body.role is not None else target.role
    destination = db.get(Company, destination_id)
    if target.email != body.email or destination_id != source_id:
        suffix = "@" + body.email.rsplit("@", 1)[1]
        if suffix not in destination.email_suffixes:
            raise HTTPException(422, "请选择所属公司当前可用的邮箱后缀")
    if next_role == PLATFORM_ADMIN:
        if actor.role != PLATFORM_ADMIN:
            raise HTTPException(403, "只有平台超管可以分配「平台超管」角色")
        if not destination.allow_platform_admin:
            raise HTTPException(409, "目标公司未开放平台超管权限，请调整用户角色")
    if next_role != target.role or (destination_id != source_id and target.role == "company_admin"):
        ensure_other_admin(db, target)
    if target.email != body.email or destination_id != source_id or (body.username is not None and body.username != target.username):
        old_email, old_username = target.email, target.username
        change_user_email(db, target, body.email, company_id=destination_id, username=body.username)
        if old_email != body.email:
            changes.append(f"邮箱从 {old_email} 修改为 {body.email}")
        if target.username != old_username:
            changes.append(f"用户名从 {old_username} 修改为 {target.username}")
    if destination_id != source_id:
        changes.append(f"所属公司从 {db.get(Company, source_id).name} 修改为 {destination.name}")
        target.company_id = destination_id
    if target.role != next_role:
        changes.append(f"将角色从{ROLE_LABELS[target.role]}调整为{ROLE_LABELS[next_role]}")
        target.role = next_role
    if "phone" in body.model_fields_set and target.phone != body.phone:
        old = target.phone or '未填写'
        new = body.phone or '未填写'
        changes.append(f"电话从 {mask_phone(old) if old != '未填写' else old} 修改为 {mask_phone(new) if new != '未填写' else new}")
        target.phone = body.phone
    if changes:
        record_operation(db, actor, target, "user.update", "；".join(changes))
        commit_account_change(db)
    return ok(user_data(db, target))


@router.delete("/users/{user_id}")
def delete_user(user_id: int, confirmed_empty_company_id: int | None = Query(None, gt=0),
                actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if user_id == actor.id:
        raise HTTPException(409, "不能删除自己")
    target = get_target(db, actor, user_id, lock=True)
    ensure_batch_admins(db, [target], allow_empty_companies=actor.role == PLATFORM_ADMIN)
    confirm_company_emptying(db, [target], [confirmed_empty_company_id] if confirmed_empty_company_id else [])
    record_operation(db, actor, target, "user.delete", f"删除用户 {target.username}")
    db.delete(target)
    commit_account_change(db)
    return ok()


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: PasswordReset, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = get_target(db, actor, user_id, lock=True)
    target.password_hash = hash_password(body.password)
    record_operation(db, actor, target, "user.reset_password", f"重置了 {target.username} 的密码")
    commit_account_change(db)
    return ok()


@router.put("/users/{user_id}/role")
def assign_role(user_id: int, body: RoleUpdate, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = get_target(db, actor, user_id, lock=True)
    if body.role == PLATFORM_ADMIN:
        if actor.role != PLATFORM_ADMIN:
            raise HTTPException(403, "只有平台超管可以分配「平台超管」角色")
        company = db.get(Company, target.company_id)
        if company is None or not company.allow_platform_admin:
            raise HTTPException(403, "该用户所属公司不允许设置为平台超管")
    if target.role != body.role:
        ensure_other_admin(db, target)
        old = target.role
        target.role = body.role
        record_operation(db, actor, target, "user.assign_role", f"将角色从{ROLE_LABELS[old]}调整为{ROLE_LABELS[body.role]}")
        commit_account_change(db)
    return ok(user_data(db, target))


@router.get("/operation-logs")
def operation_logs(page_num: int = Query(1, alias="page", ge=1), page_size: int = Query(50, ge=1, le=100),
                   company_id: int | None = Query(None, gt=0), operator: str = Query("", max_length=100),
                   target: str = Query("", max_length=200), target_username: str = Query("", max_length=100),
                   action: Literal["user.create", "user.update", "user.delete", "user.reset_password", "user.assign_role", "company.add_email_suffix", "company.delete_email_suffix", "company.create", "company.update", "company.delete"] | None = None,
                   start: datetime | None = None, end: datetime | None = None,
                   actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start and end and start >= end:
        raise HTTPException(422, "开始时间必须早于结束时间")
    if actor.role != PLATFORM_ADMIN and company_id not in (None, actor.company_id):
        return page([], 0, page_num, page_size)
    visible_company = actor.company_id if actor.role != PLATFORM_ADMIN else company_id
    conditions = [operation_log_scope(db, visible_company, target_username or target)]
    if actor.role != PLATFORM_ADMIN:
        conditions.append(OperationLog.operator_role == "company_admin")
    if target_username:
        conditions.append(OperationLog.action.startswith("user.", autoescape=True))
    if operator:
        conditions.append(OperationLog.operator_username.contains(operator, autoescape=True))
    if action:
        conditions.append(OperationLog.action == action)
    if start:
        conditions.append(OperationLog.created_at >= start)
    if end:
        conditions.append(OperationLog.created_at <= end)
    total = db.scalar(select(func.count()).select_from(OperationLog).where(*conditions))
    rows = db.execute(select(OperationLog, Company.name).join(Company, Company.id == OperationLog.company_id)
                      .where(*conditions).order_by(OperationLog.id.desc()).offset((page_num - 1) * page_size).limit(page_size))
    items = [operation_log_data(log, name, visible_company) for log, name in rows]
    return page(items, total, page_num, page_size)
