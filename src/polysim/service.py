"""Application service coordinating Polymarket data and SQLite state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .markets import Market, MarketDataError, MarketEvent, PolymarketClient
from .store import Account, InvalidStake, Position, SaleResult, Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketQuote:
    market: Market
    yes_buy: Decimal | None
    no_buy: Decimal | None


@dataclass(frozen=True)
class SaleQuote:
    position: Position
    price: Decimal
    shares_to_sell: Decimal
    proceeds: Decimal
    cost_basis: Decimal

    @property
    def remaining_shares(self) -> Decimal:
        return self.position.remaining_shares - self.shares_to_sell


@dataclass(frozen=True)
class Portfolio:
    account: Account
    positions: list[Position]
    position_value: Decimal
    unpriced_positions: int

    @property
    def total_value(self) -> Decimal:
        return self.account.balance + self.position_value


class BettingService:
    def __init__(self, store: Store, markets: PolymarketClient) -> None:
        self.store = store
        self.markets = markets

    async def search(self, query: str, limit: int = 10) -> list[MarketQuote]:
        markets = await self.markets.search_markets(query, limit)
        return await self._quote_markets(markets)

    async def search_events(self, query: str, limit: int = 10) -> list[MarketEvent]:
        return await self.markets.search_events(query, limit)

    async def trending_events(self, limit: int = 100) -> list[MarketEvent]:
        return await self.markets.trending_events(limit)

    async def ending_soon_markets(
        self, limit: int = 100, *, days: int = 7, query: str | None = None
    ) -> list[Market]:
        return await self.markets.ending_soon_markets(
            limit, days=days, query=query
        )

    async def event_markets(
        self, event_id: str, *, offset: int = 0, limit: int = 15
    ) -> tuple[MarketEvent, list[MarketQuote]]:
        event = await self.markets.get_event(event_id)
        ranked = sorted(
            event.markets,
            key=lambda market: market.outcome_prices.get("yes", Decimal("0")),
            reverse=True,
        )
        return event, await self._quote_markets(ranked[offset : offset + limit])

    async def popular(self, limit: int = 10) -> list[MarketQuote]:
        markets = await self.markets.popular_markets(limit)
        return await self._quote_markets(markets)

    async def _quote_markets(self, markets: list[Market]) -> list[MarketQuote]:
        token_ids = [token for market in markets for token in market.outcome_tokens.values()]
        # /price BUY returns the bid; buying a share crosses to the ask (side=SELL).
        prices = await self.markets.get_prices(token_ids, "SELL")
        return [
            MarketQuote(
                market=market,
                yes_buy=prices.get(market.outcome_tokens["yes"]),
                no_buy=prices.get(market.outcome_tokens["no"]),
            )
            for market in markets
        ]

    async def place_bet(
        self,
        *,
        guild_id: str,
        user_id: str,
        market_id: str,
        channel_id: str,
        outcome: str,
        stake_value: str,
    ) -> Position:
        market, normalized, stake, price = await self.quote_bet(
            market_id=market_id,
            outcome=outcome,
            stake_value=stake_value,
        )
        token_id = market.outcome_tokens[normalized]

        return self.store.place_bet(
            guild_id=guild_id,
            user_id=user_id,
            market_id=market.id,
            channel_id=channel_id,
            question=market.question,
            market_url=market.url,
            outcome=normalized,
            token_id=token_id,
            entry_price=price,
            stake=stake,
        )

    async def quote_bet(
        self, *, market_id: str, outcome: str, stake_value: str
    ) -> tuple[Market, str, Decimal, Decimal]:
        normalized = outcome.strip().lower()
        if normalized not in {"yes", "no"}:
            raise InvalidStake("Choose YES or NO")
        try:
            stake = Decimal(stake_value)
        except (InvalidOperation, TypeError) as exc:
            raise InvalidStake("Enter a valid credit amount") from exc
        if not stake.is_finite() or stake <= 0:
            raise InvalidStake("Stake must be greater than zero")

        market = await self.markets.get_market(market_id)
        if not market.is_open:
            raise InvalidStake("That market is no longer open for simulated bets")
        token_id = market.outcome_tokens[normalized]
        # Use the ask as the simulated taker buy price.
        price = await self.markets.get_price(token_id, "SELL")
        if price is None:
            raise MarketDataError("There is no current BUY quote for that outcome")
        return market, normalized, stake, price

    def account(self, guild_id: str, user_id: str) -> Account:
        return self.store.account(guild_id, user_id)

    def positions(self, guild_id: str, user_id: str) -> list[Position]:
        return self.store.list_positions(guild_id, user_id)

    def bet_history(self, guild_id: str, user_id: str, limit: int = 10) -> list[Position]:
        return self.store.bet_history(guild_id, user_id, limit)

    async def quote_sale(
        self,
        *,
        guild_id: str,
        user_id: str,
        position_id: int,
        amount_value: str,
    ) -> SaleQuote:
        position = self.store.open_position(guild_id, user_id, position_id)
        if position is None:
            raise InvalidStake("That position is no longer open or does not belong to you")
        value = amount_value.strip().lower()
        try:
            if value == "all":
                shares_to_sell = position.remaining_shares
            elif value.endswith("%"):
                percentage = Decimal(value[:-1].strip())
                if not percentage.is_finite() or not Decimal("0") < percentage <= Decimal("100"):
                    raise InvalidStake("Enter a percentage greater than 0 and no more than 100")
                shares_to_sell = position.remaining_shares * percentage / Decimal("100")
            else:
                shares_to_sell = Decimal(value)
        except (InvalidOperation, TypeError) as exc:
            raise InvalidStake("Enter shares, a percentage such as 25%, or 'all'") from exc
        if not shares_to_sell.is_finite() or shares_to_sell <= 0:
            raise InvalidStake("Shares to sell must be greater than zero")
        if shares_to_sell > position.remaining_shares:
            raise InvalidStake(
                f"You can sell up to {position.remaining_shares:.4f} remaining shares"
            )
        # A simulated sale hits the bid (side=BUY on Polymarket's /price endpoint).
        price = await self.markets.get_price(position.token_id, "BUY")
        if price is None:
            raise MarketDataError("There is no current SELL quote for that position")
        if shares_to_sell == position.remaining_shares:
            cost_basis = position.remaining_stake
        else:
            cost_basis = (
                position.remaining_stake
                * shares_to_sell
                / position.remaining_shares
            )
        return SaleQuote(
            position=position,
            price=price,
            shares_to_sell=shares_to_sell,
            proceeds=shares_to_sell * price,
            cost_basis=cost_basis,
        )

    async def sell_position(
        self,
        *,
        guild_id: str,
        user_id: str,
        position_id: int,
        shares_to_sell: Decimal,
    ) -> SaleResult:
        position = self.store.open_position(guild_id, user_id, position_id)
        if position is None:
            raise InvalidStake("That position is no longer open or does not belong to you")
        price = await self.markets.get_price(position.token_id, "BUY")
        if price is None:
            raise MarketDataError("There is no current SELL quote for that position")
        return self.store.sell_position(
            guild_id=guild_id,
            user_id=user_id,
            position_id=position_id,
            shares_to_sell=shares_to_sell,
            price=price,
        )

    async def position_values(self, positions: list[Position]) -> dict[int, Decimal | None]:
        open_positions = [position for position in positions if position.status == "open"]
        prices = await self.markets.get_prices(
            [position.token_id for position in open_positions], "BUY"
        )
        return {
            position.id: (
                position.remaining_shares * price if (price := prices.get(position.token_id)) is not None else None
            )
            for position in open_positions
        }

    async def portfolio(self, guild_id: str, user_id: str) -> Portfolio:
        account = self.store.account(guild_id, user_id)
        positions = self.store.open_positions(guild_id)
        positions = [position for position in positions if position.user_id == user_id]
        token_ids = [position.token_id for position in positions]
        prices = await self.markets.get_prices(token_ids, "BUY")
        value = Decimal("0")
        unpriced = 0
        for position in positions:
            price = prices.get(position.token_id)
            if price is None:
                unpriced += 1
                continue
            value += position.remaining_shares * price
        return Portfolio(account, positions, value, unpriced)

    async def leaderboard(self, guild_id: str) -> list[tuple[Account, Decimal, int]]:
        accounts = self.store.leaderboard_accounts(guild_id)
        open_positions = self.store.open_positions(guild_id)
        prices = await self.markets.get_prices(
            [position.token_id for position in open_positions], "BUY"
        )
        totals: dict[str, Decimal] = {account.user_id: account.balance for account in accounts}
        missing: dict[str, int] = {account.user_id: 0 for account in accounts}
        for position in open_positions:
            price = prices.get(position.token_id)
            if price is None:
                missing[position.user_id] = missing.get(position.user_id, 0) + 1
            else:
                totals[position.user_id] = totals.get(position.user_id, Decimal("0")) + (
                    position.remaining_shares * price
                )
        ranked = [
            (account, totals[account.user_id], missing[account.user_id]) for account in accounts
        ]
        return sorted(ranked, key=lambda row: row[1], reverse=True)

    def reset(self, guild_id: str, user_id: str | None = None) -> tuple[int, int]:
        return self.store.reset(guild_id, user_id)

    async def settle_open_markets(self) -> list[Position]:
        open_positions = self.store.open_positions()
        market_ids = sorted({position.market_id for position in open_positions})
        results = await asyncio.gather(
            *(self._settle_market(market_id) for market_id in market_ids),
            return_exceptions=True,
        )
        return [position for result in results if isinstance(result, list) for position in result]

    async def _settle_market(self, market_id: str) -> list[Position]:
        try:
            market = await self.markets.get_market(market_id)
        except MarketDataError:
            logger.exception("Unable to refresh market %s for settlement", market_id)
            return []
        if not market.resolved:
            return []
        return self.store.settle_market(market.id, market.outcome_prices)
