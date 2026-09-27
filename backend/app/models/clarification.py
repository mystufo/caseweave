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
    # clarifying / awaiting_clarification / awaiting_answers / generating / awaiting_test_points / done / error
    # awaiting_clarification: 文档已 persist，等用户确认要注入到 Clarifier 的知识库条目
    # awaiting_test_points: 两阶段生成的第一步已跑完，功能点清单在 test_points 里等用户确认/修改

    # 两阶段生成的功能点清单草稿 [{sub, feature, scope, priority}]；用户在面板上改完后
    # 随 /api/generate 的 test_points 传回，生成时再覆盖成最终版（便于回看用户改了什么）。
    test_points = Column(JSONB, nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
