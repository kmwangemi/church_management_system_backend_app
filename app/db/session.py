from typing import AsyncGenerator

from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# DATABASE_URL must use the async driver: postgresql+asyncpg://user:pass@host/db
# (providers often hand out "postgres://" or "postgresql://", which fails with asyncpg).
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.ENVIRONMENT == "DEVELOPMENT",
    pool_pre_ping=True,
    # The two cache settings below are for PgBouncer / Supabase-style poolers in
    # transaction mode. If you connect straight to Postgres you can drop connect_args.
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        # With PgBouncer you may also need unique prepared-statement names:
        # "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",  # from uuid import uuid4
    },
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # autoflush=False: queries do NOT see pending (un-flushed) objects.
    # After session.add(...) call `await session.flush()` before running a query that
    # must see it (e.g. generating the next membership number inside a loop).
    autoflush=False,
    expire_on_commit=False,  # needed for async: avoids lazy reloads after commit
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
