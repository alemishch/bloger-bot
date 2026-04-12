"""AmoCRM v4 client for contact lookup/create and token refresh."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from bot.config import settings

logger = structlog.get_logger()


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


@dataclass
class AmoContact:
    contact_id: str
    created: bool


class AmoCRMClient:
    def __init__(self) -> None:
        domain = settings.AMOCRM_DOMAIN.strip()
        self._base_url = f"https://{domain}/api/v4"
        self._token_url = f"https://{domain}/oauth2/access_token"
        self._access_token = settings.AMOCRM_ACCESS_TOKEN.strip()
        self._refresh_token = settings.AMOCRM_REFRESH_TOKEN.strip()
        self._client_id = settings.AMOCRM_CLIENT_ID.strip()
        self._client_secret = settings.AMOCRM_CLIENT_SECRET.strip()
        self._redirect_uri = settings.AMOCRM_REDIRECT_URI.strip()

    @property
    def enabled(self) -> bool:
        return bool(settings.AMOCRM_ENABLED and settings.AMOCRM_DOMAIN)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _refresh_access_token(self) -> bool:
        required = [
            self._refresh_token,
            self._client_id,
            self._client_secret,
            self._redirect_uri,
        ]
        if not all(required):
            return False

        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "redirect_uri": self._redirect_uri,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(self._token_url, json=payload)
            if resp.status_code >= 400:
                logger.warning("amocrm_refresh_failed", status=resp.status_code, body=resp.text[:500])
                return False
            data = resp.json()
            self._access_token = data.get("access_token", self._access_token)
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            return True

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, f"{self._base_url}{path}", headers=self._headers(), **kwargs)
            if resp.status_code == 401 and await self._refresh_access_token():
                resp = await client.request(method, f"{self._base_url}{path}", headers=self._headers(), **kwargs)
            return resp

    async def find_contact_by_phone(self, phone: str) -> str | None:
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        resp = await self._request("GET", "/contacts", params={"query": normalized, "limit": 1})
        if resp.status_code >= 400:
            logger.warning("amocrm_contact_search_failed", status=resp.status_code, body=resp.text[:500])
            return None
        contacts = (resp.json().get("_embedded") or {}).get("contacts") or []
        if not contacts:
            return None
        return str(contacts[0]["id"])

    async def create_contact(self, name: str | None, phone: str, email: str | None = None) -> str | None:
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        custom_fields = [
            {"field_code": "PHONE", "values": [{"value": f"+{normalized}"}]},
        ]
        if email:
            custom_fields.append({"field_code": "EMAIL", "values": [{"value": email}]})

        payload = [{
            "name": name or "Telegram user",
            "custom_fields_values": custom_fields,
        }]
        resp = await self._request("POST", "/contacts", json=payload)
        if resp.status_code >= 400:
            logger.warning("amocrm_contact_create_failed", status=resp.status_code, body=resp.text[:500])
            return None
        created = (resp.json().get("_embedded") or {}).get("contacts") or []
        if not created:
            return None
        return str(created[0]["id"])

    async def update_contact(self, contact_id: str, name: str | None, phone: str | None, email: str | None) -> bool:
        custom_fields = []
        normalized = normalize_phone(phone or "")
        if normalized:
            custom_fields.append({"field_code": "PHONE", "values": [{"value": f"+{normalized}"}]})
        if email:
            custom_fields.append({"field_code": "EMAIL", "values": [{"value": email}]})

        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if custom_fields:
            payload["custom_fields_values"] = custom_fields
        if not payload:
            return True

        resp = await self._request("PATCH", f"/contacts/{contact_id}", json=payload)
        if resp.status_code >= 400:
            logger.warning("amocrm_contact_update_failed", status=resp.status_code, body=resp.text[:500])
            return False
        return True

    async def create_deal(
        self,
        *,
        contact_id: str,
        name: str,
        pipeline_id: int | None = None,
        status_id: int | None = None,
        price: int | None = None,
    ) -> str | None:
        lead: dict[str, Any] = {
            "name": name,
            "_embedded": {"contacts": [{"id": int(contact_id)}]},
        }
        if pipeline_id is not None:
            lead["pipeline_id"] = int(pipeline_id)
        if status_id is not None:
            lead["status_id"] = int(status_id)
        if price is not None:
            lead["price"] = int(price)

        resp = await self._request("POST", "/leads", json=[lead])
        if resp.status_code >= 400:
            logger.warning("amocrm_deal_create_failed", status=resp.status_code, body=resp.text[:500])
            return None
        leads = (resp.json().get("_embedded") or {}).get("leads") or []
        if not leads:
            return None
        return str(leads[0]["id"])

    async def find_or_create_contact(
        self,
        *,
        name: str | None,
        phone: str | None,
        email: str | None = None,
    ) -> AmoContact | None:
        if not self.enabled or not phone:
            return None
        existing_id = await self.find_contact_by_phone(phone)
        if existing_id:
            await self.update_contact(existing_id, name=name, phone=phone, email=email)
            return AmoContact(contact_id=existing_id, created=False)
        created_id = await self.create_contact(name=name, phone=phone, email=email)
        if created_id:
            return AmoContact(contact_id=created_id, created=True)
        return None
