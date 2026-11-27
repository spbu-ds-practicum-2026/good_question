from dataclasses import dataclass
from typing import Annotated

from fastapi import Header

from catalog_service.errors import DomainError


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str


def principal(
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
) -> Principal:
    # Same trusted-Gateway header contract used by Maxim's shared.py.
    if not x_user_id or not x_user_role:
        raise DomainError(401, "unauthorized", "Требуются заголовки доверенного Gateway")
    if len(x_user_id) > 128 or not x_user_id.strip():
        raise DomainError(401, "unauthorized", "Некорректный идентификатор пользователя")
    if x_user_role not in {"user", "admin"}:
        raise DomainError(403, "forbidden", "Неизвестная роль")
    return Principal(x_user_id, x_user_role)


def require_admin(user: Principal) -> None:
    if user.role != "admin":
        raise DomainError(403, "forbidden", "Требуется роль admin")
