import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from app.core.config import settings
from fastapi import HTTPException, status
from loguru import logger


def upload_to_cloudinary(
    file_bytes: bytes,
    content_type: str = "image/png",
    folder: str = "church_logos",
    resource_type: str = "image",
) -> Dict[str, Any]:
    """
    Upload a file to Cloudinary using signed REST API.
    Does not require external SDK dependencies.
    """
    cloud_name = settings.CLOUDINARY_CLOUD_NAME
    api_key = settings.CLOUDINARY_API_KEY
    api_secret = settings.CLOUDINARY_API_SECRET

    if not cloud_name or not api_key or not api_secret:
        logger.error("Cloudinary credentials are not properly configured in settings/.env")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cloudinary storage is not configured on the server",
        )

    timestamp = int(time.time())

    # Build signed parameters
    params_to_sign = {
        "folder": folder,
        "timestamp": str(timestamp),
    }

    sorted_keys = sorted(params_to_sign.keys())
    to_sign_str = "&".join(f"{k}={params_to_sign[k]}" for k in sorted_keys) + api_secret
    signature = hashlib.sha1(to_sign_str.encode("utf-8")).hexdigest()

    # Encode file bytes into data URI
    b64_data = base64.b64encode(file_bytes).decode("utf-8")
    data_uri = f"data:{content_type};base64,{b64_data}"

    # Prepare form post body
    payload = {
        "file": data_uri,
        "api_key": api_key,
        "timestamp": timestamp,
        "folder": folder,
        "signature": signature,
    }

    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"
    encoded_data = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=encoded_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode("utf-8")
            result = json.loads(resp_body)
            logger.info(f"Successfully uploaded to Cloudinary: {result.get('secure_url')}")
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        logger.error(f"Cloudinary upload failed (HTTP {e.code}): {error_body}")
        try:
            err_json = json.loads(error_body)
            msg = err_json.get("error", {}).get("message", "Cloudinary upload failed")
        except Exception:
            msg = error_body or "Cloudinary upload failed"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloudinary upload error: {msg}",
        )
    except Exception as exc:
        logger.error(f"Cloudinary upload network error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect to Cloudinary: {str(exc)}",
        )

