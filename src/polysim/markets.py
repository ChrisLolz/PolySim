"""Read-only client for Polymarket's public market-data APIs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class MarketDataError(RuntimeError):
    """Raised when Polymarket data cannot be fetched or parsed."""


@dataclass(frozen=True)
class Market:
    id: str
    question: str
    slug: str
    active: bool
    closed: bool
    outcome_tokens: dict[str, str]
    outcome_prices: dict[str, Decimal]
    accepting_orders: bool = True
    enable_order_book: bool = True
    game_start_time: datetime | None = None
    volume_24h: Decimal = Decimal("0")
    event_title: str | None = None
    volume_all_time: Decimal = Decimal("0")
    option_label: str | None = None
    end_date: datetime | None = None
    event_id: str | None = None
    event_slug: str | None = None

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{quote(self.slug, safe='')}"

    @property
    def resolved(self) -> bool:
        if not self.closed or not self.outcome_prices:
            return False
        values = list(self.outcome_prices.values())
        return all(Decimal("0") <= value <= Decimal("1") for value in values) and abs(
            sum(values, Decimal("0")) - Decimal("1")
        ) <= Decimal("0.001")

    @property
    def is_open(self) -> bool:
        return (
            self.active
            and not self.closed
            and self.accepting_orders
            and self.enable_order_book
            # Sports markets can retain stale active/order flags after a game
            # starts. The browse and paper-bet flow only includes pregame bets.
            and (
                self.game_start_time is None
                or self.game_start_time > datetime.now(timezone.utc)
            )
        )

    @classmethod
    def from_gamma(
        cls,
        data: dict[str, Any],
        *,
        event_title: str | None = None,
        event_game_start_time: Any = None,
        event_end_date: Any = None,
    ) -> Market | None:
        outcomes = _json_list(data.get("outcomes"))
        token_ids = _json_list(data.get("clobTokenIds", data.get("clob_token_ids")))
        prices = _json_list(data.get("outcomePrices", data.get("outcome_prices")))
        if not outcomes or not token_ids or len(outcomes) != len(token_ids):
            return None

        token_by_outcome: dict[str, str] = {}
        price_by_outcome: dict[str, Decimal] = {}
        for index, (label, token) in enumerate(zip(outcomes, token_ids, strict=True)):
            normalized = str(label).strip().lower()
            token_by_outcome[normalized] = str(token)
            if index < len(prices):
                try:
                    price_by_outcome[normalized] = Decimal(str(prices[index]))
                except InvalidOperation:
                    pass

        # This MVP deliberately accepts ordinary binary YES/NO markets only.
        if not {"yes", "no"}.issubset(token_by_outcome):
            return None

        raw_events = data.get("events")
        nested_events = [raw_events] if isinstance(raw_events, dict) else _json_list(raw_events)
        parent_event = next(
            (event for event in nested_events if isinstance(event, dict)), None
        )
        if not event_title and parent_event:
            event_title = str(parent_event.get("title") or parent_event.get("name") or "")
        event_title = event_title or data.get("eventTitle") or data.get("event_title")

        closed = _bool(data.get("closed"))
        active = _bool(data.get("active"), default=not closed)
        game_start_time = _parse_datetime(
            data.get("gameStartTime", data.get("game_start_time"))
            or event_game_start_time
        )
        return cls(
            id=str(data.get("id", "")),
            question=str(data.get("question") or data.get("title") or "Untitled market"),
            slug=str(data.get("slug") or data.get("id") or ""),
            active=active,
            closed=closed,
            outcome_tokens=token_by_outcome,
            outcome_prices=price_by_outcome,
            accepting_orders=_bool(
                data.get("acceptingOrders", data.get("accepting_orders")),
                default=active and not closed,
            ),
            enable_order_book=_bool(
                data.get("enableOrderBook", data.get("enable_order_book")),
                default=True,
            ),
            game_start_time=game_start_time,
            volume_24h=_decimal(
                data.get("volume24hrClob", data.get("volume24hr", data.get("volume_24hr")))
            ),
            event_title=str(event_title) if event_title else None,
            volume_all_time=_decimal(data.get("volumeNum", data.get("volume"))),
            option_label=(
                str(data.get("groupItemTitle") or data.get("group_item_title") or "").strip()
                or None
            ),
            end_date=_parse_datetime(
                data.get("endDate", data.get("end_date")) or event_end_date
            ),
            event_id=(str(parent_event.get("id")) if parent_event and parent_event.get("id") else None),
            event_slug=(
                str(parent_event.get("slug"))
                if parent_event and parent_event.get("slug")
                else None
            ),
        )


@dataclass(frozen=True)
class MarketEvent:
    id: str
    title: str
    slug: str
    markets: list[Market]
    volume_all_time: Decimal = Decimal("0")
    volume_24h: Decimal = Decimal("0")
    end_date: datetime | None = None

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{quote(self.slug, safe='')}"


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no"}
    return bool(value)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PolymarketClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._http = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _get_json(self, base: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.get(f"{base}{path}", **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(f"Could not fetch Polymarket data ({path})") from exc

    @staticmethod
    def _parse_event(data: dict[str, Any]) -> MarketEvent | None:
        event_id = str(data.get("id") or "")
        if not event_id:
            return None
        if _bool(data.get("closed")) or not _bool(data.get("active"), default=True):
            return None
        title = str(data.get("title") or data.get("name") or "Untitled event")
        game_start_time = data.get("gameStartTime", data.get("game_start_time"))
        event_end_date = data.get("endDate", data.get("end_date"))
        markets = []
        for raw_market in data.get("markets", []):
            if not isinstance(raw_market, dict):
                continue
            market = Market.from_gamma(
                raw_market,
                event_title=title,
                event_game_start_time=game_start_time,
                event_end_date=event_end_date,
            )
            if market and market.is_open:
                markets.append(market)
        if not markets:
            return None
        all_time_volume = _decimal(data.get("volume", data.get("volumeNum")))
        volume_24h = _decimal(data.get("volume24hr", data.get("volume24hrClob")))
        return MarketEvent(
            id=event_id,
            title=title,
            slug=str(data.get("slug") or event_id),
            markets=markets,
            volume_all_time=all_time_volume or sum(
                (market.volume_all_time for market in markets), Decimal("0")
            ),
            volume_24h=volume_24h or sum(
                (market.volume_24h for market in markets), Decimal("0")
            ),
            end_date=min(
                (market.end_date for market in markets if market.end_date is not None),
                default=_parse_datetime(event_end_date),
            ),
        )

    async def trending_events(self, limit: int = 100) -> list[MarketEvent]:
        payload = await self._get_json(
            GAMMA_BASE,
            "/events",
            params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": min(max(limit, 10), 100),
            },
        )
        if not isinstance(payload, list):
            return []
        events = [
            event
            for raw_event in payload
            if isinstance(raw_event, dict)
            if (event := self._parse_event(raw_event)) is not None
        ]
        return sorted(events, key=lambda event: event.volume_24h, reverse=True)[:limit]

    async def ending_soon_markets(
        self, limit: int = 100, *, days: int = 7, query: str | None = None
    ) -> list[Market]:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        if query and query.strip():
            events = await self.search_events(query.strip(), limit=limit)
            markets = [
                market
                for event in events
                for market in event.markets
                if market.end_date is not None and now < market.end_date <= cutoff
            ]
            return sorted(markets, key=lambda market: market.volume_all_time, reverse=True)[
                :limit
            ]

        page_limit = min(max(limit, 1), 100)
        markets: list[Market] = []
        seen_ids: set[str] = set()
        for offset in range(0, 2001, page_limit):
            payload = await self._get_json(
                GAMMA_BASE,
                "/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volumeNum",
                    "ascending": "false",
                    "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_date_max": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": page_limit,
                    "offset": offset,
                },
            )
            if not isinstance(payload, list) or not payload:
                break
            for raw_market in payload:
                if not isinstance(raw_market, dict):
                    continue
                market = Market.from_gamma(raw_market)
                if (
                    market is None
                    or not market.is_open
                    or not market.event_id
                    or market.end_date is None
                    or not now < market.end_date <= cutoff
                    or market.id in seen_ids
                ):
                    continue
                markets.append(market)
                seen_ids.add(market.id)
                if len(markets) >= limit:
                    break
            if len(payload) < page_limit or len(markets) >= limit:
                break
        return sorted(markets, key=lambda market: market.volume_all_time, reverse=True)[:limit]

    async def search_events(self, query: str, limit: int = 10) -> list[MarketEvent]:
        payload = await self._get_json(
            GAMMA_BASE,
            "/public-search",
            params={
                "q": query,
                # The API defaults this to only a handful of events. `limit`
                # is ignored by public-search; use its per-type limit instead.
                "limit_per_type": min(max(limit, 25), 100),
                "events_status": "active",
            },
        )
        raw_events = payload.get("events", []) if isinstance(payload, dict) else []
        events: list[MarketEvent] = []
        seen_ids: set[str] = set()
        for raw_event in raw_events:
            if not isinstance(raw_event, dict) or not raw_event.get("id"):
                continue
            event_id = str(raw_event["id"])
            if event_id in seen_ids:
                continue
            data = raw_event
            if not raw_event.get("markets"):
                data = await self._get_json(
                    GAMMA_BASE, f"/events/{quote(event_id, safe='')}"
                )
                if isinstance(data, dict) and not data.get("title"):
                    data["title"] = raw_event.get("title") or raw_event.get("name")
                if isinstance(data, dict) and not data.get("slug"):
                    data["slug"] = raw_event.get("slug")
            if not isinstance(data, dict):
                continue
            event = self._parse_event(data)
            if event:
                events.append(event)
                seen_ids.add(event.id)
                if len(events) >= limit:
                    break
        return sorted(
            events,
            key=lambda event: (event.volume_all_time, event.volume_24h),
            reverse=True,
        )

    async def get_event(self, event_id: str) -> MarketEvent:
        data = await self._get_json(
            GAMMA_BASE, f"/events/{quote(event_id, safe='')}"
        )
        if not isinstance(data, dict):
            raise MarketDataError("Polymarket returned an invalid event")
        event = self._parse_event(data)
        if event is None:
            raise MarketDataError("This event has no active supported YES/NO markets")
        return event

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        payload = await self._get_json(GAMMA_BASE, "/public-search", params={"q": query})
        events = payload.get("events", []) if isinstance(payload, dict) else []
        event_data: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("markets"), list):
                event_data.append(event)
            elif event.get("id"):
                event_data.append(await self._get_json(GAMMA_BASE, f"/events/{event['id']}"))

        markets: list[Market] = []
        seen_ids: set[str] = set()
        for event in event_data:
            parent_title = event.get("title") or event.get("name")
            for raw_market in event.get("markets", []):
                if not isinstance(raw_market, dict):
                    continue
                market = Market.from_gamma(
                    raw_market, event_title=str(parent_title) if parent_title else None
                )
                if market and market.id not in seen_ids and market.is_open:
                    markets.append(market)
                    seen_ids.add(market.id)
                    if len(markets) >= limit:
                        return markets
        return markets

    async def popular_markets(self, limit: int = 10) -> list[Market]:
        markets: list[Market] = []
        seen_ids: set[str] = set()
        cursor: str | None = None
        while len(markets) < limit:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": max(limit * 5, 50),
                "order": "volume24hr",
                "ascending": "false",
            }
            if cursor:
                params["after_cursor"] = cursor
            payload = await self._get_json(GAMMA_BASE, "/markets/keyset", params=params)
            if isinstance(payload, list):
                raw_markets = payload
                cursor = None
            elif isinstance(payload, dict):
                raw_markets = payload.get("markets", [])
                cursor = payload.get("next_cursor") or payload.get("nextCursor")
            else:
                break

            for data in raw_markets:
                if not isinstance(data, dict):
                    continue
                market = Market.from_gamma(data)
                if (
                    market is not None
                    and market.is_open
                    and market.id not in seen_ids
                ):
                    markets.append(market)
                    seen_ids.add(market.id)
            if len(markets) >= limit or not cursor or not raw_markets:
                break

        markets.sort(key=lambda market: market.volume_24h, reverse=True)
        return markets[:limit]

    async def get_market(self, market_id: str) -> Market:
        data = await self._get_json(GAMMA_BASE, f"/markets/{quote(market_id)}")
        if not isinstance(data, dict):
            raise MarketDataError("Polymarket returned an invalid market")
        market = Market.from_gamma(data)
        if market is None:
            raise MarketDataError("This market is not a supported binary YES/NO market")
        return market

    async def get_price(self, token_id: str, side: str) -> Decimal | None:
        """Read the requested book side: BUY is the bid; SELL is the ask."""
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        payload = await self._get_json(
            CLOB_BASE,
            "/price",
            params={"token_id": token_id, "side": side},
        )
        try:
            price = Decimal(str(payload["price"]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise MarketDataError("Polymarket returned an invalid price") from exc
        return price if Decimal("0") < price <= Decimal("1") else None

    async def get_prices(self, token_ids: list[str], side: str) -> dict[str, Decimal | None]:
        unique_ids = list(dict.fromkeys(token_ids))
        results = await asyncio.gather(
            *(self.get_price(token_id, side) for token_id in unique_ids), return_exceptions=True
        )
        prices: dict[str, Decimal | None] = {}
        for token_id, result in zip(unique_ids, results, strict=True):
            if isinstance(result, Exception):
                prices[token_id] = None
            else:
                prices[token_id] = result
        return prices
