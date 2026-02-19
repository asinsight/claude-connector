#!/usr/bin/env python3
"""
Image analysis using the Anthropic Claude API.
Uses curl for the HTTP request — no external Python packages required.
"""

import base64
import json
import logging
import os
import subprocess

MIME_MAP = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
}

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def analyze_image_with_vision(image_path: str, user_prompt: str, config: dict) -> str:
    """
    Send image_path to the Anthropic Vision API and return the model's response.
    Returns a user-friendly error string if the API key is missing or the call fails.
    """
    api_key = config.get("anthropic_api_key", "").strip()
    if not api_key:
        return (
            "[Image analysis unavailable: no API key configured. "
            "Set anthropic_api_key in config.json to enable.]"
        )

    # Convert HEIC to JPEG before encoding
    ext = os.path.splitext(image_path)[1].lower()
    if ext in (".heic", ".heif"):
        from file_handler import convert_heic_to_jpg
        image_path = convert_heic_to_jpg(image_path)
        ext = os.path.splitext(image_path)[1].lower()

    # Read and base64-encode the image
    try:
        with open(image_path, "rb") as fh:
            image_data = base64.standard_b64encode(fh.read()).decode("utf-8")
    except OSError as exc:
        logging.error("Cannot read image %s: %s", image_path, exc)
        return f"[Cannot read image file: {exc}]"

    media_type = MIME_MAP.get(ext, "image/jpeg")
    model = config.get("vision_model", "claude-sonnet-4-5-20250514")
    prompt_text = user_prompt.strip() if user_prompt else "Describe this image in detail."

    payload = json.dumps({
        "model": model,
        "max_tokens": 2048,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {"type": "text", "text": prompt_text},
            ],
        }],
    })

    logging.info("Calling Vision API for %s (model=%s)", os.path.basename(image_path), model)

    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST", API_URL,
                "-H", f"x-api-key: {api_key}",
                "-H", f"anthropic-version: {API_VERSION}",
                "-H", "content-type: application/json",
                "-d", "@-",
            ],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "[Vision API call timed out]"
    except Exception as exc:
        logging.error("Vision API subprocess error: %s", exc)
        return f"[Vision API error: {exc}]"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"[Failed to parse API response: {result.stdout[:200]}]"

    if "content" in data and data["content"]:
        return data["content"][0].get("text", "[Empty response]")
    if "error" in data:
        msg = data["error"].get("message", "Unknown API error")
        logging.error("Vision API error: %s", msg)
        return f"[API error: {msg}]"

    return f"[Unexpected API response: {result.stdout[:200]}]"
