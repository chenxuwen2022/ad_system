"""Run from repository root: python -m wellflow.bootstrap_accounts --help.

Passwords are read interactively (or from AUTH_BOOTSTRAP_PASSWORD for automation),
never supplied as CLI arguments or printed. Re-running never resets an account.
"""
import argparse
import getpass
import os
import sys

from sqlalchemy import select

from wellflow.app.database import SessionLocal
from wellflow.app.models.account_models import Company, User
from wellflow.app.schemas.account_schemas import normalize_email, normalize_username, validate_password
from wellflow.app.services.accounts import insert_user


def bootstrap(db, *, company_name, username, email, role, password):
    username = normalize_username(username)
    email = normalize_email(email)
    validate_password(password)
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        company = db.get(Company, existing.company_id)
        if existing.role != role or company.name != company_name or existing.deleted_at is not None:
            raise ValueError("该邮箱已关联其他身份或已删除，请使用另一个邮箱")
        return existing, False
    company = db.scalar(select(Company).where(Company.name == company_name, Company.deleted_at.is_(None)).order_by(Company.id).with_for_update())
    if company is None:
        company = Company(name=company_name)
        db.add(company)
        db.flush()
    if role == "platform_admin" and not company.allow_platform_admin:
        raise ValueError(f"公司「{company_name}」未开放平台超管权限，请先在数据库开启 allow_platform_admin")
    user = insert_user(db, company_id=company.id, username=username, email=email, password=password, role=role)
    db.commit()
    return user, True


def main():
    parser = argparse.ArgumentParser(description="初始化平台超管或公司首位管理员")
    parser.add_argument("--email", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--company", default="数语深流")
    parser.add_argument("--role", choices=["platform_admin", "company_admin"], required=True)
    args = parser.parse_args()
    password = os.environ.get("AUTH_BOOTSTRAP_PASSWORD") or getpass.getpass("初始密码：")
    with SessionLocal() as db:
        try:
            user, created = bootstrap(db, company_name=args.company, username=args.username, email=args.email, role=args.role, password=password)
        except ValueError as exc:
            parser.exit(1, str(exc) + "\n")
        sys.stdout.write(f"{'已创建' if created else '已存在（未修改）'}：用户名 {user.username}，角色 {user.role}，公司 ID {user.company_id}\n")


if __name__ == "__main__":
    main()
