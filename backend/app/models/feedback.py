from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    case_number = Column(String(50), nullable=False)     # e.g. TC-LOGIN-001
    name = Column(String(255), nullable=False)
    module = Column(String(100), nullable=True)
    preconditions = Column(Text, nullable=True)
    steps = Column(Text, nullable=False)
    expected_result = Column(Text, nullable=False)
    remarks = Column(Text, nullable=True)
    priority = Column(String(2), nullable=True, default="P2")  # P1 / P2 / P3
    test_result = Column(String(20), nullable=True)      # pass / fail / blocked (left empty)
    prompt_version = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("Session", back_populates="test_cases")
    feedbacks = relationship("Feedback", back_populates="test_case", cascade="all, delete-orphan")


class Feedback(Base):
    __tablename__ = "feedbacks"

    id = Column(Integer, primary_key=True, index=True)
    test_case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False)
    feedback_type = Column(String(20), nullable=False)  # like / dislike / edit
    original_content = Column(JSON, nullable=True)      # snapshot before edit
    modified_content = Column(JSON, nullable=True)      # snapshot after edit
    diff_analysis = Column(Text, nullable=True)         # LLM analysis of the diff
    reason = Column(Text, nullable=True)                # dislike 可选原因文本（进化链路 3）
    triage = Column(String(20), nullable=True)          # 归一后的 intent（补充边界用例/修正业务规则/…）
    triage_targets = Column(String(64), nullable=True)  # 分诊出口，逗号分隔如 "prompt,skill"；空=不消费
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    test_case = relationship("TestCase", back_populates="feedbacks")


class FeedbackConsumption(Base):
    """负反馈消费台账（进化闭环）。

    记录某条反馈已被哪个出口（knowledge / skill / prompt）消费、消费产物是谁。
    三条进化出口据此"只吃未消费的增量"，避免同一批反馈被反复分析（问题 B）。
    (feedback_id, output_kind) 唯一——同一反馈对同一出口只消费一次（幂等）。
    """
    __tablename__ = "feedback_consumptions"
    __table_args__ = (
        UniqueConstraint("feedback_id", "output_kind", name="uq_feedback_consumption"),
    )

    id = Column(Integer, primary_key=True, index=True)
    feedback_id = Column(Integer, ForeignKey("feedbacks.id", ondelete="CASCADE"), nullable=False, index=True)
    output_kind = Column(String(20), nullable=False)     # knowledge / skill / prompt
    output_ref_id = Column(Integer, nullable=True)        # KnowledgeEntry / Skill / PromptVersion 的 id
    created_at = Column(DateTime(timezone=True), server_default=func.now())
