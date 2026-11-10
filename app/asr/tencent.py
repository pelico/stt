"""腾讯云一句话识别（SentenceRecognition）。

VAD 已经把语音切成短句，直接用一句话识别最省额度（每月 5000 次免费）。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import requests

from app.asr.base import BaseASR

logger = logging.getLogger(__name__)

HOST = "asr.tencentcloudapi.com"
SERVICE = "asr"
VERSION = "2019-06-14"


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sign(secret_id: str, secret_key: str, action: str, region: str, payload: str) -> dict:
    """TC3-HMAC-SHA256 签名。"""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )

    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        f"{timestamp}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Version": VERSION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
    }


class TencentASR(BaseASR):
    engine_name = "tencent"

    # 一句话识别没有自动语种检测，只能按语言选引擎
    SERVICE_TYPES = {"zh": "16k_zh", "en": "16k_en", "auto": "16k_zh"}

    def __init__(self, cfg):
        self.secret_id = cfg.get("tencent_secret_id")
        self.secret_key = cfg.get("tencent_secret_key")
        self.region = cfg.get("tencent_region") or "ap-shanghai"
        self.service_type = self.SERVICE_TYPES.get(
            cfg.get("asr_language") or "auto", "16k_zh"
        )

    def _recognize_sync(self, pcm: bytes) -> str:
        audio_b64 = base64.b64encode(pcm).decode("ascii")
        body = {
            "EngSerViceType": self.service_type,
            "SourceType": 1,
            "VoiceFormat": "pcm",
            "Data": audio_b64,
            "DataLen": len(pcm),
            "ProjectId": 0,
            "SubServiceType": 2,
        }
        payload = json.dumps(body, ensure_ascii=False)
        headers = _sign(
            self.secret_id, self.secret_key, "SentenceRecognition", self.region, payload
        )

        resp = requests.post(
            f"https://{HOST}", data=payload.encode("utf-8"), headers=headers, timeout=30
        )
        resp.raise_for_status()
        data = resp.json().get("Response", {})
        if "Error" in data:
            err = data["Error"]
            raise RuntimeError(
                f"腾讯云 ASR {err.get('Code')}: {err.get('Message')}"
            )
        return (data.get("Result") or "").strip()

    async def recognize(self, pcm: bytes) -> str:
        if not self.secret_id or not self.secret_key:
            raise RuntimeError("未配置腾讯云 SecretId/SecretKey")
        try:
            return await asyncio.to_thread(self._recognize_sync, pcm)
        except Exception as e:
            # 上抛给 main.py 统一做连续失败计数与熔断退避
            logger.error("腾讯云 ASR 请求失败: %s", e)
            raise
