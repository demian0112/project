from __future__ import annotations

from functools import wraps
from hmac import compare_digest
from secrets import token_urlsafe
from typing import Any, Callable, TypeVar, cast

from flask import jsonify, redirect, request, session, url_for

from .extensions import db
from .models import Admin


ViewFunction = TypeVar("ViewFunction", bound=Callable[..., Any])


def current_admin() -> Admin | None:
    admin_id = session.get("admin_id")
    if not isinstance(admin_id, int):
        return None
    return db.session.get(Admin, admin_id)


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_is_valid() -> bool:
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get(
        "csrf_token",
    )
    return (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and compare_digest(expected, supplied)
    )


def log_in_admin(admin: Admin) -> None:
    session.clear()
    session["admin_id"] = admin.id
    session["csrf_token"] = token_urlsafe(32)


def log_out_admin() -> None:
    session.clear()


def admin_required(view_function: ViewFunction) -> ViewFunction:
    @wraps(view_function)
    def wrapped(*args: Any, **kwargs: Any):
        if current_admin() is not None:
            return view_function(*args, **kwargs)

        if request.path.startswith("/api"):
            return jsonify({"error": "administrator login required"}), 401

        return redirect(url_for("site.login"))

    return cast(ViewFunction, wrapped)
