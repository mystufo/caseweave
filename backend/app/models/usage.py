"""LLM token 用量记账表。

每个用户每天一行，被 app/usage.py 的 UsageRecorder 回调按次累加。
存在的意义是给「每日 token 配额」提供判定依据，顺便让运营方看清钱花在谁身上——
并发闸门只压峰值，真正封顶成本的是这张表撑起来的配额。
"""
from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class DailyUsage(Base):
    __tablename__ = "daily_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "day", name="uq_daily_usage_user_day"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # 自然日，按 settings.quota_reset_utc_offset_hours 换算（默认 +8，即北京时间零点翻篇）。
    day = Column(Date, nullable=False, index=True)

    input_tokens = Column(BigInteger, nullable=False, server_default="0", default=0)
    output_tokens = Column(BigInteger, nullable=False, server_default="0", default=0)
    # LLM 调用次数（不是 HTTP 请求数——一次生成可能内含分类/检索/生成好几次调用）
    calls = Column(Integer, nullable=False, server_default="0", default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
