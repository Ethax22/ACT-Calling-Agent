"""Calls to the erp-lookup service. It decides what a caller may hear (role, date of
birth check, lockout); this only carries the question and returns its answer."""
import os

import aiohttp
from loguru import logger

UNKNOWN = {"status": "unknown", "role": "unknown"}
ERP_DOWN = {"status": "error", "speech": "I can't look that up right now. I can connect you to the reception."}


class ErpClient:
    def __init__(self):
        self._url = os.environ.get("ERP_URL", "").rstrip("/")
        self._key = os.environ.get("ERP_API_KEY", "")

    async def _post(self, path: str, body: dict) -> dict | None:
        if not self._url:
            logger.warning(f"ERP_URL is not set; {path} skipped")
            return None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as http:
                async with http.post(
                    f"{self._url}{path}", json=body, headers={"Authorization": f"Bearer {self._key}"}
                ) as r:
                    if r.status != 200:
                        logger.error(f"erp-lookup {path} answered {r.status}")
                        return None
                    return await r.json()
        except Exception as e:
            logger.error(f"erp-lookup {path} failed: {e}")
            return None

    async def identify(self, caller_number: str) -> dict:
        return await self._post("/identify", {"caller_number": caller_number}) or UNKNOWN

    async def student(self, caller_number: str, reg_no: str, date_of_birth: str, topic: str, subject: str) -> dict:
        body = {"caller_number": caller_number, "reg_no": reg_no, "date_of_birth": date_of_birth,
                "topic": topic or "both", "subject": subject or ""}
        return await self._post("/student", body) or ERP_DOWN

    async def class_summary(self, caller_number: str, faculty_id: str, topic: str, class_name: str = "") -> dict:
        body = {"caller_number": caller_number, "faculty_id": faculty_id, "topic": topic or "", "class_name": class_name or ""}
        return await self._post("/class", body) or ERP_DOWN

    async def route(self, department: str) -> dict:
        return await self._post("/route", {"department": department}) or {"status": "none", "transfer_number": "none"}
