import asyncio
from decimal import Decimal

import httpx

from polysim.markets import Market, PolymarketClient


def test_market_parses_polymarket_string_encoded_fields():
    market = Market.from_gamma(
        {
            "id": 123,
            "question": "Will it happen?",
            "slug": "will-it-happen",
            "active": True,
            "closed": False,
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "outcomePrices": '["0.7", "0.3"]',
        }
    )

    assert market is not None
    assert market.id == "123"
    assert market.outcome_tokens == {"yes": "yes-token", "no": "no-token"}
    assert market.outcome_prices == {"yes": Decimal("0.7"), "no": Decimal("0.3")}
    assert not market.resolved


def test_market_requires_binary_yes_no_outcomes():
    market = Market.from_gamma(
        {
            "id": "1",
            "outcomes": '["A", "B"]',
            "clobTokenIds": '["a", "b"]',
        }
    )

    assert market is None


def test_market_is_resolved_only_when_closed_with_final_values():
    base = {
        "id": "1",
        "question": "Will it happen?",
        "slug": "test",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes", "no"]',
        "outcomePrices": '["0.5", "0.5"]',
    }

    assert not Market.from_gamma({**base, "closed": False}).resolved
    assert Market.from_gamma({**base, "closed": True}).resolved


def test_get_price_uses_public_buy_quote_endpoint():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"price": "0.42"})

    async def scenario():
        client = PolymarketClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        price = await client.get_price("token-yes", "BUY")
        await client._http.aclose()
        return price

    price = asyncio.run(scenario())

    assert price == Decimal("0.42")
    assert requests[0].url.path == "/price"
    assert requests[0].url.params["token_id"] == "token-yes"
    assert requests[0].url.params["side"] == "BUY"


def test_search_uses_public_search_and_fetches_event_markets():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/public-search":
            return httpx.Response(200, json={"events": [{"id": "event-1"}]})
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "id": "market-1",
                        "question": "Will it happen?",
                        "slug": "will-it-happen",
                        "active": True,
                        "closed": False,
                        "outcomes": '["Yes", "No"]',
                        "clobTokenIds": '["yes-token", "no-token"]',
                    }
                ]
            },
        )

    async def scenario():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = PolymarketClient(http)
        markets = await client.search_markets("question", limit=10)
        await http.aclose()
        return markets

    markets = asyncio.run(scenario())

    assert [market.id for market in markets] == ["market-1"]
    assert paths == ["/public-search", "/events/event-1"]
