"""Тесты бесплатных публичных источников без зависимости от Tool-слоя."""
import sys
import pytest
sys.path.insert(0, '.')
from core.data import public_sources as ps

class FakeResponse:
    status_code = 200
    def __init__(self, data=None, text=''):
        self.data, self.text = data, text
    def raise_for_status(self): return None
    def json(self):
        if isinstance(self.data, Exception): raise self.data
        return self.data


def test_news_rss(monkeypatch):
    xml = '<?xml version="1.0"?><rss><channel><item><title>Новость</title><link>https://x</link></item></channel></rss>'
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(text=xml))
    assert ps.news_search('технологии') == [{'title': 'Новость', 'url': 'https://x'}]


def test_news_limit(monkeypatch):
    items = ''.join(f'<item><title>{i}</title><link>https://x/{i}</link></item>' for i in range(10))
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(text=f'<rss><channel>{items}</channel></rss>'))
    assert len(ps.news_search('x', 3)) == 3


def test_news_failure_is_empty(monkeypatch):
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: (_ for _ in ()).throw(ps.requests.RequestException('down')))
    assert ps.news_search('x') == []


def test_wiki_extract(monkeypatch):
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(data={'extract': 'Кратко.'}))
    assert ps.wiki_summary('Python') == 'Кратко.'


def test_wiki_empty_is_none(monkeypatch):
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(data={}))
    assert ps.wiki_summary('Нет') is None


def test_currency_rates(monkeypatch):
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(data={'result':'success','rates':{'USD':0.01}}))
    assert ps.currency_rates('RUB')['USD'] == 0.01


def test_currency_error_is_none(monkeypatch):
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(data={'result':'error'}))
    assert ps.currency_rates('RUB') is None


def test_cbr_currency_rates(monkeypatch):
    cbr_payload = {
        "Date": "2026-09-04T11:30:00+03:00",
        "Valute": {
            "USD": {"CharCode": "USD", "Nominal": 1, "Name": "Доллар США", "Value": 89.25},
            "KZT": {"CharCode": "KZT", "Nominal": 100, "Name": "Казахстанских тенге", "Value": 18.50},
        }
    }
    monkeypatch.setattr(ps, 'safe_http_get', lambda *a, **k: FakeResponse(data=cbr_payload))
    res = ps.cbr_currency_rates()
    assert res is not None
    assert res["rates"]["USD"]["rate"] == 89.25
    assert res["rates"]["KZT"]["rate"] == 0.185


def test_empty_queries_are_safe():
    assert ps.news_search('') == []
    assert ps.wiki_summary('') is None
