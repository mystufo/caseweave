from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from app.database import Base


class ClarificationState(Base):
    """每个会话的澄清运行态 1:1 快照——刷新页面后用它复原 ChatPage 的 SessionState。"""

    __tablename__ = "clarification_states"

    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    # 测试脑图 .md 文档（与 PRD 平行的可选输入）；两者可同时存在，冲突时下游以脑图为准。
    mindmap_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)

    summary = Column(Text, nullable=True)
    module_detected = Column(String(255), nullable=True)
    case_prefix_suggestion = Column(String(64), nullable=True)
    confirmed_module_name = Column(String(255), nullable=True)
    confirmed_case_prefix = Column(String(64), nullable=True)

    current_round = Column(Integer, nullable=False, default=1)
    rounds = Column(JSONB, nullable=False, default=list)             # ClarificationRound[]
    current_questions = Column(JSONB, nullable=False, default=list)  # ClarificationQuestion[]

    ready_to_generate = Column(Boolean, nullable=False, default=False)
    status = Column(String(40), nullable=False, default="clarifying")
    # clarifying / awaiting_clarification / awaiting_answers / generating / done / error
    # awaiting_clarification: 文档已 persist，等用户确认要注入到 Clarifier 的知识库条目

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
