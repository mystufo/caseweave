from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(255), nullable=False, default="New Session")
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), default="active")  # active / completed / archived
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    test_cases = relationship("TestCase", back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)  # user / assistant / system
    content = Column(Text, nullable=False)
    meta = Column(Text, nullable=True)  # JSON: doc_chunks, clarification_questions, etc.
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("Session", back_populates="messages")
