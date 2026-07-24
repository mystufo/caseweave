from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    # LLM Provider
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-4-6"
    llm_api_key: str = ""
    llm_base_url: Optional[str] = None
    # 单次 LLM 调用的超时（秒）与重试次数。没有超时的话，provider 卡住时请求会永久挂起——
    # 前端「正在抽取产品知识…」等 loader 靠调用返回/抛错才解除，一旦挂起就界面卡死。
    # 有了超时，慢/挂起的调用会在 timeout*(1+retries) 内抛错，调用方的 fail-open 分支即可兜底。
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 1

    # 各 agent 的 max_tokens 上限。三个分开是因为输出体量需求差异很大：
    #   generator   ：要输出几十条用例的 JSON 数组，最易被截断；同时填了 PRD + 脑图 + 多轮澄清时
    #                  16384 仍可能不够，必要时调到 24576 / 32768
    #   clarifier   ：JSON 问题列表，几 KB 量级，8192 通常足够
    #   knowledge   ：抽取条目 JSON 数组，与文档体量相关，8192 一般够
    # 注意：思考模型（如 kimi-k2 / o1）的 max_tokens 通常**包含 reasoning_tokens**，给小了会先把
    # 思考 token 烧完然后正文为空 —— 不要随便往下调
    generator_max_tokens: int = 16384
    clarifier_max_tokens: int = 8192
    knowledge_max_tokens: int = 8192

    # ── Phase 4.2 二阶段：系统给 generator 提示词的改进建议 ────────────────────
    # 定期后台任务只生成 pending 草稿、绝不激活；采用仍靠人工走版本化 API。
    # interval_hours ≤ 0 关闭后台巡检（仅保留网页端手动按钮）。
    prompt_suggestion_interval_hours: float = 24.0
    # 少于该条负反馈样本就不给建议（信号太弱）。手动 / 后台共用。
    prompt_suggestion_min_samples: int = 3

    # Embedding (OpenAI-compatible /v1/embeddings; 用于 Phase 3 知识库语义检索)
    # 留空 → store 静默降级（写入跳过 embedding，搜索退化为按时间倒序），不会阻塞 LLM 主流程
    embedding_provider: str = "openai"           # 目前仅支持 openai 兼容接口
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str = ""
    embedding_base_url: Optional[str] = None     # e.g. https://ark.cn-beijing.volces.com/api/v3
    embedding_dim: int = 1536                    # 必须与上面 model 输出维度一致；默认 OpenAI small=1536
    embedding_mode: str = "standard"             # standard | multimodal（火山方舟视觉版走 /embeddings/multimodal）

    # ── Knowledge retrieval (Phase 3) ─────────────────────────────────────────
    # 全局开关，覆盖所有项目所有模块。要按项目精细化配置请先升级到 ProjectSetting 表。
    # preview: 上传/生成前给前端 KnowledgePreviewPanel 的候选条数。给得多用户能看到更全的边缘条目，
    #          但同时也意味着默认勾选会更激进，要靠用户取消。
    # inject_default: 旧路径（前端没传 knowledge_ids）自动 top-K 注入到 LLM prompt 的条数。
    #                 用户没勾选时（自动）才用这个值；用户勾完后取的是用户选的 ids，无视该参数。
    # distance_threshold: 余弦距离上限。距离 > 该值的命中直接丢弃，避免把很弱的相关条目也注入。
    #                     0.45 ≈ "相关度 ≥ 55%"，对一般文档比较稳；留 0 / 负数 = 不过滤。
    # prompt_max_chars: summarize_for_prompt 拼接的最大字符数（防 prompt 撑爆）。
    knowledge_preview_top_k: int = 8
    knowledge_inject_top_k: int = 8
    knowledge_distance_threshold: float = 0.45
    knowledge_prompt_max_chars: int = 1800

    # Database
    database_url: str = "postgresql+asyncpg://testcraft:testcraft@db:5432/testcraft"

    # LangSmith (optional, for dev debugging)
    langsmith_api_key: str = ""
    langchain_tracing_v2: bool = False
    langchain_project: str = "testcraft-ai"

    # Product site (for Browser Agent)
    product_url: Optional[str] = None
    product_credentials: Optional[str] = None  # JSON string, encrypted at rest

    # Zentao MCP
    zentao_mcp_endpoint: Optional[str] = None

    # lark-cli (Feishu doc URL import)
    lark_cli_path: str = "lark-cli"
    lark_cli_timeout_seconds: int = 60
    # 抓取身份：user（个人授权，走 auth login）| bot（应用，需 app_id/secret）。
    # 默认 bot：应用凭证不过期、不依赖 OS keychain，适合容器/服务器无人值守部署。
    lark_cli_identity: str = "bot"

    # App
    debug: bool = False
    cors_origins: str = "http://localhost:3001,http://localhost:5173"

    # 日志文件路径：非空则日志同时写到该文件（自动轮转），留空则只打到 stderr（终端）。
    # 支持 ~ 展开；相对路径以后端进程 CWD 为基准。默认写到 backend/logs/app.log。
    log_file: Optional[str] = "logs/app.log"

    # 调试：每次 LLM 调用把完整 prompt + 响应 dump 到这个目录。留空则 no-op。
    # 支持 ~ 与绝对路径；相对路径以后端进程 CWD 为基准（不建议）。
    prompt_dump_dir: Optional[str] = None

    # Auth
    admin_emails: str = ""  # comma-separated list of admin emails (case-insensitive)
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24 * 7  # 7 days

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]

    @property
    def admin_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
