"""Strict account inputs; clients cannot bind role/company through profile edits."""
import re
from typing import Annotated, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssignableRole = Literal["platform_admin", "company_admin", "company_user"]
CompanyRole = Literal["company_admin", "company_user"]


def normalize_email(value: str) -> str:
    try:
        return validate_email(value.strip().lower(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError("请输入有效邮箱") from exc


def normalize_phone(value: str | None) -> str | None:
    value = value.strip() if value is not None else ''
    if not value:
        return None
    if not re.fullmatch(r'1[3-9][0-9]{9}', value):
        raise ValueError("请输入有效的11位手机号码")
    return value


def validate_password(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 72:
        raise ValueError("密码不能为空，且不能超过 72 个 UTF-8 字节")
    return value


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LoginInput(StrictInput):
    email: str = Field(min_length=1, max_length=254)
    _email = field_validator("email")(normalize_email)
    password: str = Field(min_length=1, max_length=72, repr=False)
    _password = field_validator("password")(validate_password)


def normalize_username(value: str) -> str:
    value = value.strip().lower()
    if not value or len(value) > 100 or any(c.isspace() or c in "@,，;；" for c in value):
        raise ValueError("用户名需为1至100个字符，不能包含空白、@或分隔符")
    return value


class UserCreate(StrictInput):
    username: str = Field(min_length=1, max_length=100)
    _username = field_validator("username")(normalize_username)
    role: AssignableRole = "company_user"
    email: str = Field(max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    company_id: int | None = Field(default=None, gt=0)
    _email = field_validator("email")(normalize_email)

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, value):
        return normalize_phone(value)


class UserUpdate(StrictInput):
    username: str | None = Field(default=None, min_length=1, max_length=100)
    _username = field_validator("username")(lambda v: normalize_username(v) if v is not None else None)
    role: AssignableRole | None = None
    company_id: int | None = Field(default=None, gt=0)
    email: str = Field(max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    _email = field_validator("email")(normalize_email)
    _phone = field_validator("phone")(UserCreate.clean_phone.__func__)


class PasswordReset(StrictInput):
    password: str = Field(min_length=1, max_length=72, repr=False)
    _password = field_validator("password")(validate_password)


class RoleUpdate(StrictInput):
    role: AssignableRole


class ChangeOwnPassword(StrictInput):
    current_password: str = Field(min_length=1, max_length=72, repr=False)
    new_password: str = Field(min_length=1, max_length=72, repr=False)
    _passwords = field_validator("current_password", "new_password")(validate_password)


class UpdateOwnPhone(StrictInput):
    phone: str | None = Field(default=None, max_length=40)

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, value):
        return normalize_phone(value)


def normalize_suffix(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("@") or value.count("@") != 1:
        raise ValueError("邮箱后缀应为 @example.com 格式")
    return "@" + normalize_email("account" + value).split("@", 1)[1]


class CompanySuffixInput(StrictInput):
    suffix: str = Field(min_length=2, max_length=254)
    _suffix = field_validator("suffix")(normalize_suffix)


class BulkUserCreate(StrictInput):
    role: AssignableRole = "company_user"
    usernames: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=50)
    email_suffix: str = Field(min_length=2, max_length=254)
    company_id: int | None = Field(default=None, gt=0)
    _suffix = field_validator("email_suffix")(normalize_suffix)

    emails: list[Annotated[str, Field(max_length=254)]] = Field(min_length=1, max_length=50)

    @field_validator("usernames")
    @classmethod
    def clean_usernames(cls, values):
        names = [normalize_username(value) for value in values]
        return names

    @field_validator("emails")
    @classmethod
    def clean_emails(cls, values):
        emails = [normalize_email(value) for value in values]
        if len(set(emails)) != len(emails):
            raise ValueError("邮箱不能重复")
        return emails

    @model_validator(mode="after")
    def paired(self):
        if len(self.usernames) != len(self.emails):
            raise ValueError("用户名和邮箱必须一一对应")
        if any("@" + email.rsplit("@", 1)[1] != self.email_suffix for email in self.emails):
            raise ValueError("邮箱必须使用所选后缀")
        return self


class BulkUserIds(StrictInput):
    user_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=1, max_length=50)

    @field_validator("user_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("用户不能重复")
        return values


class BulkUserDelete(BulkUserIds):
    confirmed_empty_company_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=list, max_length=50)


class BulkRoleUpdate(BulkUserIds):
    role: AssignableRole


class CompanyInput(StrictInput):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("公司名称不能为空")
        return value


class CompanyCreate(CompanyInput):
    email_suffixes: list[Annotated[str, Field(min_length=2, max_length=254)]] = Field(min_length=1, max_length=50)

    @field_validator("email_suffixes")
    @classmethod
    def clean_suffixes(cls, values):
        return list(dict.fromkeys(normalize_suffix(value) for value in values))


class CompanyUpdate(CompanyInput):
    email_suffixes: list[Annotated[str, Field(min_length=2, max_length=254)]] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("email_suffixes")
    @classmethod
    def clean_suffixes(cls, values):
        if values is None:
            raise ValueError("邮箱后缀必须是列表")
        return list(dict.fromkeys(normalize_suffix(value) for value in values))
