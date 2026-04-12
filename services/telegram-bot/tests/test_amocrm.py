from bot.amocrm import normalize_phone
from bot.amocrm import AmoCRMClient


class _DummyClient(AmoCRMClient):
    def __init__(self):
        self.calls = []
        self._responses = []
        self._base_url = ""
        self._token_url = ""
        self._access_token = ""
        self._refresh_token = ""
        self._client_id = ""
        self._client_secret = ""
        self._redirect_uri = ""

    @property
    def enabled(self) -> bool:
        return True

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        status_code, payload = self._responses.pop(0)

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.text = str(payload)

            def json(self):
                return self._payload

        return _Resp(status_code, payload)


def test_normalize_phone_keeps_ru_prefix():
    assert normalize_phone("+7 (999) 111-22-33") == "79991112233"


def test_normalize_phone_converts_local_8_to_7():
    assert normalize_phone("8 (999) 111-22-33") == "79991112233"


def test_normalize_phone_strips_non_digits():
    assert normalize_phone("  +7-999-000 00 00 ") == "79990000000"


import pytest


@pytest.mark.anyio
async def test_find_or_create_returns_existing_contact():
    client = _DummyClient()
    client._responses = [(200, {"_embedded": {"contacts": [{"id": 123}]}}), (200, {})]
    result = await client.find_or_create_contact(name="A", phone="+7 (999) 123-45-67", email="a@test.com")
    assert result is not None
    assert result.contact_id == "123"
    assert result.created is False
    assert client.calls[0][0] == "GET"
    assert client.calls[1][0] == "PATCH"


@pytest.mark.anyio
async def test_create_deal_sends_contact_binding():
    client = _DummyClient()
    client._responses = [(200, {"_embedded": {"leads": [{"id": 777}]}})]
    lead_id = await client.create_deal(
        contact_id="123",
        name="Subscription",
        pipeline_id=1,
        status_id=2,
        price=3000,
    )
    assert lead_id == "777"
    method, path, kwargs = client.calls[0]
    assert method == "POST"
    assert path == "/leads"
    payload = kwargs["json"][0]
    assert payload["_embedded"]["contacts"][0]["id"] == 123
