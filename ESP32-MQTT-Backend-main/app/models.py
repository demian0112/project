from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


DEVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _truncate_template_value(value: str, limit: int = 20) -> str:
    value = str(value or "").strip()
    return value[:limit]


def public_fault_message(code: str | None, raw_message: str | None = None) -> str | None:
    if not code and not raw_message:
        return None
    fault_code = (code or "").strip().upper()
    if fault_code == "UART_TIMEOUT":
        return "CSI采集中断，请检查链路"
    if fault_code in {"NO_CSI_FRAME", "NO_CSI_FRAME_TIMEOUT"}:
        return "未收到CSI数据，请检查供电"
    return "设备采集异常，请检查设备"


def fault_template_data(
    *,
    device_name: str,
    display_name: str | None,
    location: str | None,
    code: str | None,
    raw_message: str | None = None,
) -> dict[str, dict[str, str]] | None:
    message = public_fault_message(code, raw_message)
    if message is None:
        return None
    return {
        "phrase1": {"value": "异常"},
        "thing3": {
            "value": _truncate_template_value(display_name or device_name)
        },
        "thing2": {"value": _truncate_template_value(location or "未设置位置")},
        "thing5": {"value": _truncate_template_value(message)},
    }


def public_fault_payload(
    *,
    device_name: str,
    display_name: str | None,
    location: str | None,
    code: str | None,
    raw_message: str | None = None,
) -> dict:
    return {
        "code": code,
        "message": public_fault_message(code, raw_message),
        "template_data": fault_template_data(
            device_name=device_name,
            display_name=display_name,
            location=location,
            code=code,
            raw_message=raw_message,
        ),
    }


class Admin(db.Model):
    __tablename__ = "admin"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class User(db.Model):
    """A mini-program user identified by the openid returned by WeChat."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    wx_openid: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )
    wx_unionid: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        nullable=True,
    )
    wx_session_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str] = mapped_column(
        String(20),
        default="user",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    devices: Mapped[list[Device]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    fall_events: Mapped[list[FallEvent]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    wx_subscriptions: Mapped[list[WxSubscription]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    wx_notify_logs: Mapped[list[WxNotifyLog]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    @property
    def admin_display_name(self) -> str:
        return self.nickname or f"微信用户 #{self.id}"

    def to_dict(self) -> dict:
        """Administrator representation; never include WeChat secrets."""
        return {
            "id": self.id,
            "nickname": self.nickname,
            "avatar_url": self.avatar_url,
            "phone": self.phone,
            "role": self.role,
            "status": self.status,
            "device_count": len(self.devices),
            "last_login_at": isoformat(self.last_login_at),
            "created_at": isoformat(self.created_at),
        }

    def to_public_dict(self) -> dict:
        """Mini-program representation; openid and session_key stay private."""
        return {
            "id": self.id,
            "nickname": self.nickname,
            "avatar_url": self.avatar_url,
            "phone": self.phone,
            "status": self.status,
            "last_login_at": isoformat(self.last_login_at),
        }


class Device(db.Model):
    """Device ownership plus the authoritative state snapshot."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_name: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        index=True,
        nullable=False,
    )
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(20),
        default="offline",
        nullable=False,
    )
    runtime_state: Mapped[str] = mapped_column(
        String(20),
        default="idle",
        nullable=False,
    )
    detection_state: Mapped[str] = mapped_column(
        String(20),
        default="idle",
        nullable=False,
    )
    current_session: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    network_quality: Mapped[str] = mapped_column(
        String(20),
        default="unknown",
        nullable=False,
    )
    fault_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fault_message: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_online_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_status_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_csi_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    owner: Mapped[User] = relationship(back_populates="devices")
    fall_events: Mapped[list[FallEvent]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
    )

    @staticmethod
    def is_valid_device_uid(device_name: str) -> bool:
        return bool(DEVICE_NAME_RE.fullmatch(device_name or ""))

    # Compatibility aliases for the existing administrator API and page.
    @property
    def device_uid(self) -> str:
        return self.device_name

    @device_uid.setter
    def device_uid(self, value: str) -> None:
        self.device_name = value

    @property
    def name(self) -> str:
        return self.display_name or self.device_name

    @name.setter
    def name(self, value: str) -> None:
        self.display_name = value

    @property
    def mqtt_topic(self) -> str:
        return f"csi/v1/devices/{self.device_name}/up/csi"

    @property
    def status(self) -> str:
        return "enabled" if self.enabled else "disabled"

    @status.setter
    def status(self, value: str) -> None:
        self.enabled = value == "enabled"

    @property
    def owner_id(self) -> int:
        return self.owner_user_id

    @owner_id.setter
    def owner_id(self, value: int) -> None:
        self.owner_user_id = value

    def to_dict(self) -> dict:
        """Administrator representation kept compatible with the current UI."""
        fault = public_fault_payload(
            device_name=self.device_name,
            display_name=self.display_name,
            location=self.location,
            code=self.fault_code,
            raw_message=self.fault_message,
        )
        owner_username = (
            self.owner.admin_display_name
            if self.owner is not None
            else f"Missing user #{self.owner_user_id}"
        )
        return {
            "id": self.id,
            "device_uid": self.device_name,
            "name": self.name,
            "mqtt_topic": self.mqtt_topic,
            "status": self.status,
            "state": self.state,
            "enabled": self.enabled,
            "runtime_state": self.runtime_state,
            "detection_state": self.detection_state,
            "current_session": self.current_session,
            "network_quality": self.network_quality,
            "fault_code": self.fault_code,
            "fault_message": fault["message"],
            "fault_template_data": fault["template_data"],
            "owner_id": self.owner_user_id,
            "owner_username": owner_username,
            "owner_missing": self.owner is None,
            "location": self.location,
            "remark": self.remark,
            "last_seen_at": isoformat(self.last_seen_at),
            "last_online_at": isoformat(self.last_online_at),
            "last_status_at": isoformat(self.last_status_at),
            "last_csi_at": isoformat(self.last_csi_at),
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
        }

    def to_summary_dict(self) -> dict:
        fault = public_fault_payload(
            device_name=self.device_name,
            display_name=self.display_name,
            location=self.location,
            code=self.fault_code,
            raw_message=self.fault_message,
        )
        return {
            "device_name": self.device_name,
            "display_name": self.display_name,
            "location": self.location,
            "state": self.state,
            "runtime_state": self.runtime_state,
            "detection_state": self.detection_state,
            "network_quality": self.network_quality,
            "last_seen_at": isoformat(self.last_seen_at),
            "fault_code": self.fault_code,
            "fault_message": fault["message"],
            "fault_template_data": fault["template_data"],
            "fault": {
                **fault,
            },
        }

    def to_detail_dict(self) -> dict:
        fault = public_fault_payload(
            device_name=self.device_name,
            display_name=self.display_name,
            location=self.location,
            code=self.fault_code,
            raw_message=self.fault_message,
        )
        return {
            "device_name": self.device_name,
            "display_name": self.display_name,
            "location": self.location,
            "remark": self.remark,
            "state": self.state,
            "enabled": self.enabled,
            "last_seen_at": isoformat(self.last_seen_at),
            "runtime": {
                "state": self.runtime_state,
                "last_status_at": isoformat(self.last_status_at),
            },
            "detection": {
                "state": self.detection_state,
                "session": self.current_session,
                "network_quality": self.network_quality,
                "last_csi_at": isoformat(self.last_csi_at),
            },
            "fault": {
                **fault,
            },
        }


class FallEvent(db.Model):
    __tablename__ = "fall_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    device_name: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
    )
    session: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        nullable=True,
    )
    result: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    network_quality: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
    )
    notified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    wechat_notified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    wechat_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    wechat_notify_errcode: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    wechat_notify_errmsg: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="fall_events")
    device: Mapped[Device] = relationship(back_populates="fall_events")

    def to_public_dict(self) -> dict:
        device_display_name = (
            self.device.display_name if self.device is not None else None
        )
        device_location = self.device.location if self.device is not None else None
        return {
            "id": self.id,
            "device_name": self.device_name,
            "display_name": device_display_name,
            "location": device_location,
            "result": self.result,
            "occurred_at": isoformat(self.occurred_at),
            "network_quality": self.network_quality,
            "status": self.status,
            "handled_at": isoformat(self.handled_at),
            "remark": self.remark,
        }

    def to_admin_dict(self) -> dict:
        item = self.to_public_dict()
        owner_name = (
            self.user.admin_display_name
            if self.user is not None
            else f"Missing user #{self.user_id}"
        )
        item.update(
            {
                "user_id": self.user_id,
                "owner_name": owner_name,
                "owner_missing": self.user is None,
                "session": self.session,
                "notified": self.notified,
                "notified_at": isoformat(self.notified_at),
                "wechat_notified": self.wechat_notified,
                "wechat_notified_at": isoformat(self.wechat_notified_at),
                "wechat_notify_errcode": self.wechat_notify_errcode,
                "wechat_notify_errmsg": self.wechat_notify_errmsg,
                "created_at": isoformat(self.created_at),
                "updated_at": isoformat(self.updated_at),
            }
        )
        return item


class WxSubscription(db.Model):
    """A user's reusable WeChat subscribe-message grant for one template."""

    __tablename__ = "wx_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "scene",
            "template_id",
            name="uq_wx_subscription_user_scene_template",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    scene: Mapped[str] = mapped_column(
        String(40),
        default="fall_alert",
        nullable=False,
    )
    template_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default="accept",
        nullable=False,
    )
    remaining_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    last_subscribed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="wx_subscriptions")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scene": self.scene,
            "template_id": self.template_id,
            "status": self.status,
            "remaining_count": self.remaining_count,
            "last_subscribed_at": isoformat(self.last_subscribed_at),
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
        }


class WxNotifyLog(db.Model):
    """A redacted audit record for each WeChat notification attempt."""

    __tablename__ = "wx_notify_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    fall_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("fall_events.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    scene: Mapped[str] = mapped_column(
        String(40),
        default="fall_alert",
        nullable=False,
    )
    template_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    openid_masked: Mapped[str | None] = mapped_column(String(32), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    errcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    errmsg: Mapped[str | None] = mapped_column(String(255), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="wx_notify_logs")
