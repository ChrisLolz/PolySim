import asyncio
from decimal import Decimal

import pytest

from polysim.markets import Market, MarketDataError
from polysim.service import BettingService
from polysim.store import InsufficientFunds, Store


class FakeMarkets:
    def __init__(self, market, prices=None):
        self.market = market
        self.prices = prices or {}

    async def search_markets(self, query, limit):
        return [self.market]

    async def get_market(self, market_id):
        return self.market

    async def get_price(self, token_id, side):
        return self.prices.get((token_id, side))

    async def get_prices(self, token_ids, side):
        return {token_id: self.prices.get((token_id, side)) for token_id in token_ids}


def sample_market(*, closed=False, active=True, prices=None):
    return Market(
        id="market-1",
        question="Will it happen?",
        slug="will-it-happen",
        active=active,
        closed=closed,
        outcome_tokens={"yes": "yes-token", "no": "no-token"},
        outcome_prices=prices or {},
    )


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "service.sqlite3")
    store.initialize()
    market = sample_market()
    api = FakeMarkets(market, {("yes-token", "SELL"): Decimal("0.2")})
    return BettingService(store, api), store, api


def test_service_uses_fresh_buy_price_and_stores_channel(service):
    betting, store, _ = service
    position = asyncio.run(
        betting.place_bet(
            guild_id="guild-1",
            user_id="user-1",
            market_id="market-1",
            channel_id="channel-4",
            outcome="yes",
            stake_value="50",
        )
    )

    assert position.entry_price == Decimal("0.2")
    assert position.shares == Decimal("2.5E+2")
    assert position.channel_id == "channel-4"
    assert store.account("guild-1", "user-1").balance == Decimal("950")


def test_service_rejects_closed_market_and_missing_buy_quote(service):
    betting, _, api = service
    api.market = sample_market(closed=True, active=False)
    with pytest.raises(ValueError, match="no longer open"):
        asyncio.run(
            betting.place_bet(
                guild_id="guild-1",
                user_id="user-1",
                market_id="market-1",
                channel_id="channel-4",
                outcome="yes",
                stake_value="50",
            )
        )

    api.market = sample_market()
    api.prices.clear()
    with pytest.raises(MarketDataError, match="no current BUY quote"):
        asyncio.run(
            betting.place_bet(
                guild_id="guild-1",
                user_id="user-1",
                market_id="market-1",
                channel_id="channel-4",
                outcome="yes",
                stake_value="50",
            )
        )


def test_service_settles_open_position_from_final_market_data(service):
    betting, store, api = service
    position = store.place_bet(
        guild_id="guild-1",
        user_id="user-1",
        market_id="market-1",
        channel_id="channel-4",
        question="Will it happen?",
        market_url="https://polymarket.com/market/will-it-happen",
        outcome="yes",
        token_id="yes-token",
        entry_price=Decimal("0.2"),
        stake=Decimal("50"),
    )
    api.market = sample_market(
        closed=True,
        active=False,
        prices={"yes": Decimal("1"), "no": Decimal("0")},
    )

    settled = asyncio.run(betting.settle_open_markets())

    assert len(settled) == 1
    assert settled[0].id == position.id
    assert settled[0].payout == Decimal("250")
    assert store.account("guild-1", "user-1").balance == Decimal("1200")


def test_bet_cannot_overdraw_even_when_user_already_has_open_stake(service):
    betting, _, _ = service
    with pytest.raises(InsufficientFunds):
        asyncio.run(
            betting.place_bet(
                guild_id="guild-1",
                user_id="user-1",
                market_id="market-1",
                channel_id="channel-4",
                outcome="yes",
                stake_value="1001",
            )
        )


def test_leaderboard_adds_open_positions_at_the_current_sell_price(service):
    betting, store, api = service
    store.place_bet(
        guild_id="guild-1",
        user_id="user-1",
        market_id="market-1",
        channel_id="channel-4",
        question="Will it happen?",
        market_url="https://polymarket.com/market/will-it-happen",
        outcome="yes",
        token_id="yes-token",
        entry_price=Decimal("0.2"),
        stake=Decimal("100"),
    )
    api.prices[("yes-token", "BUY")] = Decimal("0.3")
    store.account("guild-1", "user-2")

    ranked = asyncio.run(betting.leaderboard("guild-1"))

    assert [(account.user_id, total) for account, total, _ in ranked] == [
        ("user-1", Decimal("1050.0")),
        ("user-2", Decimal("1000")),
    ]
