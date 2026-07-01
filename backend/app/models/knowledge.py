from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
from app.database import Base
from app.config import get_settings

_EMBED_DIM = get_settings().embedding_dim


class Module(Base):
    __tablename__ = "modules"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_modules_project_name"),)

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    # code：模块英文名，同时用作该模块下测试用例的编号前缀（case_prefix，大写 A-Z/0-9/-）。
    # 可空以兼容历史模块；生成用例时有 code 就覆盖前端传入的 case_prefix。
    code = Column(String(40), nullable=True)
    description = Column(Text, nullable=True)
    parent_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    knowledge_entries = relationship("KnowledgeEntry", back_populates="module")
    documents = relationship("Document", back_populates="module")
    skills = relationship("Skill", back_populates="module")


class ModuleRelation(Base):
    __tablename__ = "module_relations"

    id = Column(Integer, primary_key=True, index=True)
    source_module_id = Column(Integer, ForeignKey("modules.id", ondelete="CASCADE"), nullable=False)
    target_module_id = Column(Integer, ForeignKey("modules.id", ondelete="CASCADE"), nullable=False)
    relation_type = Column(String(50), nullable=False)  # depends_on / triggers / shares_data
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id = Column(Integer, primary_key=True, index=True)
    # project_id 是 Phase 3 新增的硬隔离字段：跨 project 不共享知识。
    # 历史 Phase 1/2 写过的 knowledge_entries 没这一列，init_db 里有 ALTER 兼容。
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    # module_id 改为可空：允许 project-wide 的产品规则（不绑定具体模块）。
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True)
    knowledge_type = Column(String(50), nullable=False)
    # product_rule / module_relation / defect_pattern / skill / prompt_version
    content = Column(Text, nullable=False)
    source = Column(String(50), nullable=False)
    # document / user_feedback / bug_analysis / web_exploration / prompt_test
    confidence = Column(Float, default=0.5)
    version = Column(Integer, default=1)
    embedding = Column(Vector(_EMBED_DIM), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    module = relationship("Module", back_populates="knowledge_entries")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("project_id", "sha256", name="uq_documents_project_sha"),)

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    sha256 = Column(String(64), nullable=True, index=True)  # content fingerprint (unique per project)
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    file_type = Column(String(20), nullable=False)  # docx / pdf / lark_doc / lark_wiki / lark_docs / lark_sheet / mindmap_md
    source_type = Column(String(10), nullable=False, server_default="file")  # file / lark
    source_url = Column(String(500), nullable=True, index=True)  # 飞书文档原始 URL（仅 lark 来源）
    # 区分 PRD 与测试脑图（同表两种角色）：脑图代表测试人员对 PRD 二次梳理后的最终意图；
    # 当 PRD 与脑图同时存在且冲突时，下游 Clarifier/Generator 以脑图为准。
    role = Column(String(16), nullable=False, server_default="prd", index=True)  # prd / mindmap
    parsed_content = Column(JSON, nullable=True)  # structured chunks
    raw_text = Column(Text, nullable=True)
    clarification = Column(JSON, nullable=True)  # cached Clarifier output
    # 抽取出但尚未入库的知识草稿（list[dict]）：上传后由 LLM 抽取出后先暂存这里，
    # 等用户在前端勾选要保留的条目调 /api/documents/{id}/confirm_pending_knowledge
    # 才真正写入 knowledge_entries。已处理则被清空为 None。
    pending_knowledge = Column(JSON, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

    module = relationship("Module", back_populates="documents")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    module_id = Column(Integer, ForeignKey("modules.id", ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)  # Markdown
    source = Column(String(50), default="manual")  # manual / auto_generated
    version = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    module = relationship("Module", back_populates="skills")


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=True)  # 项目隔离；NULL 仅用于历史/全局兜底
    prompt_id = Column(String(100), nullable=False, index=True)  # 逻辑 key: clarifier_initial / clarifier_followup / generator
    purpose = Column(String(50), nullable=False)  # clarification / generation / review
    version = Column(String(20), nullable=False)  # 按 (project_id, prompt_id) 自增的整数序号字符串
    template = Column(Text, nullable=False)
    variables = Column(JSON, nullable=True)  # list of variable names
    positive_rate = Column(Float, default=0.0)
    usage_count = Column(Integer, default=0)
    is_active = Column(Integer, default=1)  # 1 = active
    created_by = Column(Integer, nullable=True)  # users.id（谁保存了这个版本）
    created_at = Column(DateTime(timezone=True), server_default=func.now())

