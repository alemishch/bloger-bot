from bot.config import BotSettings


def test_telegram_proxy_url_prefers_outbound(monkeypatch):
    monkeypatch.setenv("OUTBOUND_PROXY_URL", "http://out:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://https:2")
    monkeypatch.setenv("HTTP_PROXY", "http://http:3")
    s = BotSettings(_env_file=None)
    assert s.telegram_proxy_url == "http://out:1"


def test_telegram_proxy_url_falls_back_to_https(monkeypatch):
    monkeypatch.delenv("OUTBOUND_PROXY_URL", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://https-only:2")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    s = BotSettings(_env_file=None)
    assert s.telegram_proxy_url == "http://https-only:2"


def test_telegram_proxy_url_empty_when_unset(monkeypatch):
    monkeypatch.delenv("OUTBOUND_PROXY_URL", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    s = BotSettings(_env_file=None)
    assert s.telegram_proxy_url is None
