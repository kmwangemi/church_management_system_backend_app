from app.core.config import settings
from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

# Initialize limiter with a default limit
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
    storage_uri=settings.RATE_LIMIT_STORAGE_URI,
)


def setup_rate_limiting(app: FastAPI):
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # Without this middleware, default_limits are NOT applied to routes that have no
    # @limiter.limit(...) decorator.
    app.add_middleware(SlowAPIMiddleware)


# Behind a reverse proxy / load balancer, get_remote_address sees the proxy's IP, so
# every user would share one bucket. Start uvicorn with:
#   uvicorn app.main:app --proxy-headers --forwarded-allow-ips="<proxy ip>"
#
# Stricter limits on sensitive routes (the handler needs a `request: Request` argument):
#   @router.post("/login")
#   @limiter.limit("5/minute")
#   async def login(request: Request, body: LoginRequest, ...): ...
