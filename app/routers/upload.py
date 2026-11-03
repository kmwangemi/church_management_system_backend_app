import asyncio
from typing import Optional

from app.core.deps import CurrentUser
from app.core.rate_limit import limiter
from app.services.audit import queue_audit
from app.services.cloudinary import upload_to_cloudinary
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from loguru import logger

router = APIRouter()

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
CHUNK_SIZE = 1024 * 1024

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
DOCUMENT_TYPES = IMAGE_TYPES | {"application/pdf"}

# upload category -> (Cloudinary folder, allowed MIME types). The category is an allow-list:
# it used to be pasted straight into the folder name.
UPLOAD_TYPES = {
    "logo": ("church_logos", IMAGE_TYPES),
    "image": ("church_image", IMAGE_TYPES),
    "photo": ("member_photos", IMAGE_TYPES),
    "document": ("church_document", DOCUMENT_TYPES),
}


def sniff_mime(head: bytes) -> Optional[str]:
    """Real file type from the first bytes. The client-sent Content-Type is not trustworthy.
    SVG is deliberately NOT accepted: it can carry scripts. Sanitise it first if you need it.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


async def _read_limited(file: UploadFile) -> bytes:
    """Reads at most MAX_FILE_SIZE bytes, stopping as soon as the limit is passed."""
    chunks, total = [], 0
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds maximum allowed size of {MAX_FILE_SIZE // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/upload")
@router.post("/shared/upload")
@limiter.limit("20/minute")
async def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,  # was missing: anyone on the internet could upload to your Cloudinary
    file: UploadFile = File(...),
    upload_type: Optional[str] = Query(
        "logo",
        alias="type",
        description="Upload category: logo, image, photo, document",
    ),
):
    """
    Upload a file (e.g. church logo) to Cloudinary. Requires a logged-in user.
    Also set a body-size limit on your reverse proxy (e.g. nginx client_max_body_size 11m):
    the framework parses the multipart body before this function runs.
    """
    category = (upload_type or "logo").lower()
    if category not in UPLOAD_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of: {', '.join(sorted(UPLOAD_TYPES))}",
        )
    folder, allowed = UPLOAD_TYPES[category]
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_FILE_SIZE + CHUNK_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is too large",
        )
    file_bytes = await _read_limited(file)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    content_type = sniff_mime(file_bytes[:16])
    if content_type is None or content_type not in allowed:
        allowed_txt = "PNG, JPG, GIF, WEBP" + (
            " or PDF" if "application/pdf" in allowed else ""
        )
        raise HTTPException(
            status_code=400, detail=f"Unsupported file. Allowed: {allowed_txt}"
        )
    try:
        # upload_to_cloudinary is blocking (urllib); run it off the event loop.
        result = await asyncio.to_thread(
            upload_to_cloudinary,
            file_bytes=file_bytes,
            content_type=content_type,
            folder=folder,
            resource_type="image" if content_type.startswith("image/") else "auto",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Unexpected upload error: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to process file upload"
        )  # no internal details
    queue_audit(
        background_tasks,
        request,
        "UPLOAD_FILE",
        "FILE",
        result.get("public_id"),
        actor_user_id=current_user.id,
        metadata_json={
            "type": category,
            "bytes": len(file_bytes),
            "content_type": content_type,
        },
    )
    return {
        "success": True,
        "message": "File uploaded successfully",
        "data": {
            "url": result.get("url"),
            "secure_url": result.get("secure_url"),
            "public_id": result.get("public_id"),
            "resource_type": result.get("resource_type", "image"),
            "format": result.get("format", ""),
            "bytes": result.get("bytes", len(file_bytes)),
            "width": result.get("width"),
            "height": result.get("height"),
            "created_at": result.get("created_at", ""),
        },
    }
