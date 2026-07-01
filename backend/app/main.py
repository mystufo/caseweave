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
from app.api.routes_knowledge import router as knowledge_router
from app.api.routes_feedback import router as feedback_router
from app.api.routes_auth import router as auth_router
from app.api.routes_projects import router as projects_router
from app.api.routes_prompts import router as prompts_router

settings = get_settings()
setup_logging("DEBUG" if settings.debug else "INFO")
logger = logging.getLogger("testcraft.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TestCraft AI | provider=%s model=%s", settings.llm_provider, settings.llm_model)
    await init_db()
    logger.info("Database initialized")
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="TestCraft AI",
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
app.include_router(knowledge_router, prefix="/api", tags=["knowledge"])
app.include_router(feedback_router, prefix="/api", tags=["feedback"])
app.include_router(prompts_router, prefix="/api", tags=["prompts"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "testcraft-ai"}
