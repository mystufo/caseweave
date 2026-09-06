"""管理员在网页上改的运行时配置（覆盖 .env）。

一行一个键，value 统一存字符串（bool/数字也序列化成字符串，读出时按 Settings 字段类型还原）。
密钥类字段（api_key）落库前用 app/settings_store.py 的对称加密包一层，is_secret 标记之。
哪些键允许放进这张表由 settings_store.EDITABLE 白名单决定——启动引导类配置（DATABASE_URL、
JWT_SECRET、ADMIN_EMAILS…）永远不会出现在这里，见该模块顶部注释。
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    is_secret = Column(Boolean, nullable=False, server_default="false", default=False)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
