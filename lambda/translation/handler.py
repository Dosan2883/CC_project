"""AWS Lambda Function URL handler for Bedrock based translation."""

import base64
import hmac
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MODEL_VERSION = "v1"
MAX_TOKENS = 4000
MAX_INPUT_CHARS = 4000
DEFAULT_MODEL_ID = "amazon.nova-micro-v1:0"

_secret = None
_ssm = None
_bedrock = None


def _json_response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _read_secret():
    global _secret, _ssm
    if _secret is not None:
        return _secret

    parameter_name = os.environ.get("TRANSLATE_SECRET_PARAMETER", "").strip()
    if not parameter_name:
        raise RuntimeError("TRANSLATE_SECRET_PARAMETER is not configured")
    if _ssm is None:
        _ssm = boto3.client("ssm")
    response = _ssm.get_parameter(Name=parameter_name, WithDecryption=True)
    value = response.get("Parameter", {}).get("Value", "")
    if not value:
        raise RuntimeError("translation secret parameter is empty")
    _secret = value
    return _secret


def _parse_body(event):
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, dict):
        payload = body
    elif isinstance(body, str):
        payload = json.loads(body)
    else:
        raise ValueError("invalid request body")
    if not isinstance(payload, dict):
        raise ValueError("invalid request body")
    text = payload.get("text")
    target = payload.get("target")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS:
        raise ValueError("invalid text")
    if not isinstance(target, str) or not target.strip() or len(target) > 40:
        raise ValueError("invalid target")
    return text.strip(), target.strip()


def _translate(text, target, action="translate"):
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime")

    instruction = ("Summarize only the selected evidence in the target language. Include decisions and action items only if explicitly present. Cite evidence using [1], [2], etc. Do not invent facts. Return only the summary with citations."
                   if action == "summarize" else
                   "Translate the user's text into the requested target language. Preserve meaning, tone, names, numbers and paragraph breaks. Return only the translated text without explanation.")
    response = _bedrock.converse(
        modelId=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        system=[{"text": instruction}],
        messages=[{
            "role": "user",
            "content": [{"text": f"Target language: {target}\n\nText:\n{text}"}],
        }],
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0.1},
    )

    content = response.get("output", {}).get("message", {}).get("content", [])
    translated = "".join(item.get("text", "") for item in content if isinstance(item, dict)).strip()
    if not translated:
        raise ValueError("Bedrock returned an empty translation")

    stop_reason = response.get("stopReason")
    return {
        "translated": translated,
        "finishReason": stop_reason,
        "truncated": stop_reason == "max_tokens",
        "modelVersion": MODEL_VERSION,
    }


def handler(event, context):
    """Translate a JSON request from a Lambda Function URL.

    Required request header: x-api-secret
    Required body fields: text, target
    """
    event = event if isinstance(event, dict) else {}
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
    if method and method.upper() != "POST":
        return _json_response(405, {"error": "method not allowed"})

    headers = event.get("headers") or {}
    provided_secret = next(
        (value for key, value in headers.items() if str(key).casefold() == "x-api-secret"),
        "",
    )
    try:
        expected_secret = _read_secret()
    except Exception:
        logger.exception("Translation secret could not be loaded")
        return _json_response(503, {"error": "translation unavailable"})

    if not isinstance(provided_secret, str) or not hmac.compare_digest(provided_secret, expected_secret):
        return _json_response(403, {"error": "forbidden"})

    try:
        text, target = _parse_body(event)
        raw_body = event.get("body", "{}")
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        payload = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        action = payload.get("action", "translate")
        if action not in ("translate", "summarize"):
            raise ValueError("invalid action")
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, base64.binascii.Error):
        return _json_response(400, {"error": "invalid request"})

    try:
        return _json_response(200, _translate(text, target, action))
    except Exception:
        logger.exception("Bedrock translation request failed")
        return _json_response(502, {"error": "translation unavailable"})
