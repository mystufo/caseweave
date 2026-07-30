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
    # provider:
    #   local  → 默认。指向随 docker-compose 起的本地 TEI 容器（BAAI/bge-m3，1024 维），
    #            无需 api_key，base_url 留空时自动用 http://embedding:80/v1，开箱即用。
    #   openai → 远程 OpenAI 兼容接口（含火山方舟/智谱等），必须配 embedding_api_key；
    #            不配 key 则静默降级（写入跳过 embedding，检索退化为按时间倒序），不阻塞主流程。
    embedding_provider: str = "local"            # local（本地 TEI）| openai（远程兼容接口）
    embedding_model: str = "bge-m3"              # TEI 不校验此值仅占位；远程时填实际模型名
    embedding_api_key: str = ""                  # provider=local 无需；provider=openai 必填
    embedding_base_url: Optional[str] = None     # local 留空→http://embedding:80/v1；远程 e.g. https://ark.cn-beijing.volces.com/api/v3
    embedding_dim: int = 1024                    # 必须与 model 实际输出维度一致；bge-m3=1024，OpenAI small=1536
    embedding_mode: str = "standard"             # standard | multimodal（火山方舟视觉版走 /embeddings/multimodal）

    # ── Rerank（精排，检索第二阶段）─────────────────────────────────────────
    # 单向量召回（bi-encoder）负责「捞得全」，精排把 query 与每条候选一起判定相关性负责「排得准」，
    # 是修好「整篇文档 query vs 单句知识」不对称问题的关键一环。全程 fail-open：
    # 开关关 / 未配置 / 服务未就绪 / 出错 → 自动降级回纯向量检索，绝不阻塞主流程。
    #   provider=llm    → 默认。复用主 LLM（build_chat_model，即 llm_* 那套凭证）一次调用给候选打分，
    #                     不需新服务/新鉴权；准确性略逊 cross-encoder，但零额外部署。
    #   provider=local  → 本地 TEI cross-encoder 容器（BAAI/bge-reranker-v2-m3），走 http://reranker:80/rerank。
    #   provider=openai → 远程兼容 /rerank 接口（TEI 格式），需 rerank_api_key。
    rerank_enabled: bool = True                  # 精排总开关，默认开启
    rerank_provider: str = "llm"                 # llm（复用主模型打分）| local（本地 TEI）| openai（远程 TEI）
    rerank_model: str = "bge-reranker-v2-m3"     # 仅 local/openai 用；llm 走 llm_model
    rerank_api_key: str = ""                     # provider=openai 必填；llm/local 无需
    rerank_base_url: Optional[str] = None        # local 留空→http://reranker:80；openai 填实际地址
    rerank_candidate_k: int = 30                 # 召回池大小（喂给精排的候选条数，放宽召回宁多勿漏）
    rerank_score_threshold: float = 0.3          # 精排分数下限（保留 score≥该值）；0/负=不过滤

    # ── 知识检索 query 分块（配合 max-sim 召回）───────────────────────────────
    # 把文档 query 切成窗口分别 embed，对每条知识取「跨所有窗口的最小距离」，
    # 避免长文平均向量把单句知识的相关度稀释掉。
    knowledge_query_chunk_size: int = 256        # 单个分块窗口的目标最大字符数
    knowledge_query_chunk_overlap: int = 48      # 相邻窗口重叠字符数（防关键句被边界劈开）

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
    database_url: str = "postgresql+asyncpg://caseweave:caseweave@db:5432/caseweave"

    # LangSmith (optional, for dev debugging)
    langsmith_api_key: str = ""
    langchain_tracing_v2: bool = False
    langchain_project: str = "caseweave"

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

    # ── 视觉模型（图片 → 文字）─────────────────────────────────────────────────
    # 用于把飞书文档里的内嵌图片（UI 原型 / 流程图 / 架构图）识别成文字描述，
    # append 进 Document.raw_text 一并喂给 Clarifier/Generator。默认关闭。
    # 与主 LLM 解耦：vision_* 留空时逐项回退到对应的 llm_*（见 llm_factory.build_vision_model）。
    # 要开启需 provider 侧模型支持多模态输入（如火山方舟 doubao-vision / Anthropic Claude）。
    vision_enabled: bool = False
    vision_provider: str = ""              # 空 → 回退 llm_provider（anthropic|openai）
    vision_model: str = ""                 # 空 → 回退 llm_model；建议填视觉模型/接入点
    vision_api_key: str = ""               # 空 → 回退 llm_api_key
    vision_base_url: Optional[str] = None  # 空 → 回退 llm_base_url
    vision_max_tokens: int = 1024          # 单张图描述的输出上限，够写一段结构化描述
    vision_max_images: int = 20            # 单文档识别图片张数上限（控成本/耗时）；超出丢弃并记日志
    vision_concurrency: int = 3            # 图片识别并发数（视觉接口通常限流，别开太大）

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
        # .env 里还有一批仅供 lark-cli 子进程读取的变量（LARK_APP_ID/SECRET/
        # NODE_DIR/CLI_HOME 等），它们不是本模型的字段。默认 forbid 会因这些
        # 未声明变量直接启动失败，故显式改为 ignore。
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
