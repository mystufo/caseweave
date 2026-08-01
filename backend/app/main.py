import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.config import get_settings
from app.logging_config import setup_logging
from app.api.routes_chat import router as chat_router
from app.api.routes_upload import router as upload_router
from app.api.routes_generate import router as generate_router
from app.api.routes_mindmap import router as mindmap_router
from app.api.routes_knowledge import router as knowledge_router
from app.api.routes_feedback import router as feedback_router
from app.api.routes_auth import router as auth_router
from app.api.routes_projects import router as projects_router
from app.api.routes_prompts import router as prompts_router

settings = get_settings()
setup_logging("DEBUG" if settings.debug else "INFO", settings.log_file)
logger = logging.getLogger("caseweave.main")


async def _prompt_suggestion_loop() -> None:
    """定期给每个有 generator 负反馈的项目生成一条 pending 改进建议（只写草稿、绝不激活）。

    纯 asyncio 周期任务，不引调度依赖。全异常吞掉只 log —— 后台巡检失败绝不能影响主服务。
    interval ≤ 0 时本任务根本不会被启动（见 lifespan）。
    """
    interval_s = max(0.0, settings.prompt_suggestion_interval_hours) * 3600
    # 启动后先等一个周期再跑，避免和启动期抢资源
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            break
        try:
            from sqlalchemy import select
            from app.database import AsyncSessionLocal
            from app.models.feedback import Feedback, TestCase
            from app.api.routes_prompts import run_generator_suggestion

            async with AsyncSessionLocal() as db:
                # 有过 dislike/edit 负反馈（且已分析）的项目才值得巡检
                project_ids = (await db.execute(
                    select(TestCase.project_id)
                    .join(Feedback, Feedback.test_case_id == TestCase.id)
                    .where(
                        Feedback.feedback_type.in_(("dislike", "edit")),
                        Feedback.diff_analysis.is_not(None),
                    )
                    .distinct()
                )).scalars().all()

            for pid in project_ids:
                try:
                    async with AsyncSessionLocal() as db:
                        r = await run_generator_suggestion(
                            db, pid, min_samples=settings.prompt_suggestion_min_samples,
                        )
                    if r.get("created"):
                        logger.info("后台生成 prompt 建议 | project=%s", pid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("后台 prompt 建议失败 project=%s: %s", pid, exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompt 建议巡检循环异常: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting CaseWeave | provider=%s model=%s", settings.llm_provider, settings.llm_model)
    if settings.jwt_secret_is_ephemeral:
        logger.warning(
            "未配置 JWT_SECRET，已生成进程内随机密钥：本次重启后所有登录态失效，"
            "多 worker 部署下登录会直接不可用。生产请在 .env 设置 "
            "JWT_SECRET（openssl rand -hex 32）",
        )
    await init_db()
    logger.info("Database initialized")

    suggestion_task: asyncio.Task | None = None
    if settings.prompt_suggestion_interval_hours > 0:
        suggestion_task = asyncio.create_task(_prompt_suggestion_loop())
        logger.info(
            "Prompt 建议后台任务已启动 | 间隔=%.1fh", settings.prompt_suggestion_interval_hours,
        )
    else:
        logger.info("Prompt 建议后台任务已禁用（interval ≤ 0，仅保留手动触发）")

    yield

    if suggestion_task is not None:
        suggestion_task.cancel()
        try:
            await suggestion_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutting down")


app = FastAPI(
    title="CaseWeave 纬策",
    description="Intelligent Test Case Generation System",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_logger(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %d (%.0fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


app.include_router(auth_router, prefix="/api", tags=["auth"])
app.include_router(projects_router, prefix="/api", tags=["projects"])
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(upload_router, prefix="/api", tags=["upload"])
app.include_router(generate_router, prefix="/api", tags=["generate"])
app.include_router(mindmap_router, prefix="/api", tags=["mindmap"])
app.include_router(knowledge_router, prefix="/api", tags=["knowledge"])
app.include_router(feedback_router, prefix="/api", tags=["feedback"])
app.include_router(prompts_router, prefix="/api", tags=["prompts"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "caseweave"}
