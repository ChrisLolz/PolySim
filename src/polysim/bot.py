"""Discord slash-command interface for the paper-betting service."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings
from .markets import MarketDataError, PolymarketClient
from .service import BettingService
from .store import InsufficientFunds, InvalidStake, Position, Store

logger = logging.getLogger(__name__)


def _credits(value: Decimal) -> str:
    return f"{value:,.2f}"


def _volume_millions(value: Decimal) -> str:
    return f"${value / Decimal('1000000'):,.1f}M"


def _open_position_value(position, current_value: Decimal | None) -> str:
    if current_value is None:
        return "Current value unavailable"
    unrealized_change = current_value - position.remaining_stake
    realized_change = position.realized_proceeds - position.realized_cost_basis
    change = unrealized_change + realized_change
    sign = "+" if change > 0 else ""
    percent = change / position.stake * 100
    percent_sign = "+" if percent > 0 else ""
    current_sell_price = current_value / position.remaining_shares
    odds_change = (current_sell_price - position.entry_price) * 100
    odds_sign = "+" if odds_change > 0 else ""
    realized_detail = (
        f"Realized P/L {('+' if realized_change > 0 else '')}{_credits(realized_change)} · "
        if position.realized_proceeds
        else ""
    )
    return (
        f"Odds {position.entry_price * 100:.2f}% → {current_sell_price * 100:.2f}% "
        f"({odds_sign}{odds_change:.2f} pp) · Remaining value {_credits(current_value)} · "
        f"{realized_detail}Total P/L {sign}{_credits(change)} credits ({percent_sign}{percent:.1f}%)"
    )


def _history_line(position: Position, current_value: Decimal | None) -> str:
    market_link = f"[{_safe(position.question, limit=110)}]({position.market_url})"
    date = (position.resolved_at or position.opened_at)[:10]
    if position.status == "open":
        detail = (
            f"Open · Bet {_credits(position.stake)} credits · "
            f"{position.remaining_shares:.4f}/{position.shares:.4f} shares remaining · "
            f"{_open_position_value(position, current_value)}"
        )
    elif position.status == "settled":
        payout = position.payout or Decimal("0")
        proceeds = position.realized_proceeds
        profit = proceeds + payout - position.stake
        sign = "+" if profit > 0 else ""
        detail = (
            f"Settled · Bet {_credits(position.stake)} · "
            f"Sold proceeds {_credits(proceeds)} · Payout {_credits(payout)} · "
            f"P/L {sign}{_credits(profit)} credits"
        )
    elif position.status == "sold":
        proceeds = position.realized_proceeds or position.payout or Decimal("0")
        profit = proceeds - position.stake
        sign = "+" if profit > 0 else ""
        price = proceeds / position.shares if position.shares else Decimal("0")
        detail = (
            f"Sold · Bet {_credits(position.stake)} · "
            f"{position.entry_price * 100:.2f}% → {price * 100:.2f}% · "
            f"Proceeds {_credits(proceeds)} · "
            f"P/L {sign}{_credits(profit)} credits"
        )
    else:
        detail = f"Voided by server reset · Bet {_credits(position.stake)} credits"
    return f"`#{position.id}` · {market_link}\n**{position.outcome.upper()}** · {detail} · {date}"


def _safe(text: str, limit: int = 180) -> str:
    text = text.replace("@", "@\u200b")
    for character in "[]()<>":
        text = text.replace(character, f"\\{character}")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _notice_embed(
    description: str,
    *,
    title: str = "PolySim",
    color: discord.Color | None = None,
) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.blurple(),
    )


def _settlement_notice(
    user_id: str,
    outcome: str,
    question: str,
    market_url: str,
    outcome_value: Decimal,
    stake: Decimal,
    payout: Decimal,
) -> tuple[str, discord.Embed]:
    embed = discord.Embed(
        title=_safe(question, limit=256),
        url=market_url,
        color=discord.Color.green() if payout > 0 else discord.Color.red(),
    )
    embed.add_field(name="Your pick", value=outcome.upper(), inline=True)
    embed.add_field(name="Resolved at", value=f"{outcome_value:.0%}", inline=True)
    embed.add_field(name="Bet", value=f"{_credits(stake)} credits", inline=True)
    embed.add_field(name="Payout", value=f"{_credits(payout)} credits", inline=True)
    return f"<@{user_id}>", embed


def _option_label(question: str) -> str:
    """Reduce a binary-market question to the option it represents."""
    value = question.strip().removesuffix("?")
    if value.lower().startswith("will "):
        value = value[5:].strip()
        lowered = value.lower()
        for marker in (" win ", " be ", " become ", " receive ", " get ", " lead "):
            split_at = lowered.find(marker)
            if split_at > 0:
                return value[:split_at].strip()
    return value


def _chunks(lines: list[str], limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def create_bot(settings: Settings) -> commands.Bot:
    intents = discord.Intents.default()
    store = Store(settings.database_path)
    market_client = PolymarketClient()
    service = BettingService(store, market_client)

    class PolySimBot(commands.Bot):
        settlement_task: asyncio.Task[None] | None = None

        async def setup_hook(self) -> None:
            store.initialize()
            if settings.dev_guild_id:
                guild = discord.Object(id=settings.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()

            async def settle_loop() -> None:
                while not self.is_closed():
                    try:
                        settled = await service.settle_open_markets()
                        if settled:
                            logger.info("Settled %s simulated position(s)", len(settled))
                            for position in settled:
                                channel = self.get_channel(int(position.channel_id))
                                if channel is None:
                                    try:
                                        channel = await self.fetch_channel(int(position.channel_id))
                                    except discord.HTTPException:
                                        logger.exception(
                                            "Could not find settlement channel %s",
                                            position.channel_id,
                                        )
                                        continue
                                payout = position.payout or Decimal("0")
                                try:
                                    content, embed = _settlement_notice(
                                        position.user_id,
                                        position.outcome,
                                        position.question,
                                        position.market_url,
                                        position.outcome_value or Decimal("0"),
                                        position.stake,
                                        payout,
                                    )
                                    await channel.send(
                                        content,
                                        embed=embed,
                                        allowed_mentions=discord.AllowedMentions(
                                            users=True, roles=False, everyone=False
                                        ),
                                    )
                                except discord.HTTPException:
                                    logger.exception(
                                        "Could not announce settlement for position %s", position.id
                                    )
                    except Exception:
                        logger.exception("Resolution polling failed")
                    await asyncio.sleep(settings.resolution_poll_seconds)

            self.settlement_task = asyncio.create_task(settle_loop())

        async def close(self) -> None:
            if self.settlement_task:
                self.settlement_task.cancel()
                try:
                    await self.settlement_task
                except asyncio.CancelledError:
                    pass
            await market_client.close()
            await super().close()

    bot = PolySimBot(command_prefix="!", intents=intents)

    async def ensure_public_context(interaction: discord.Interaction) -> str | None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                embed=_notice_embed("Use this command in a Discord server."), ephemeral=False
            )
            return None
        return str(interaction.guild_id)

    async def handle_error(interaction: discord.Interaction, message: str) -> None:
        embed = _notice_embed(message, title="Command failed", color=discord.Color.red())
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=False)

    @bot.tree.error
    async def app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error(
            "Unhandled slash command error",
            exc_info=(type(error), error, error.__traceback__),
        )
        await handle_error(interaction, "The command could not be completed. Please try again.")

    def event_embed(event, results, page: int) -> discord.Embed:
        page_count = max(1, (len(event.markets) + 14) // 15)
        first = (page - 1) * 15 + 1
        last = min(page * 15, len(event.markets))
        embed = discord.Embed(
            title=_safe(event.title, limit=250),
            url=event.url,
        )
        lines = []
        for result in results:
            yes = f"{result.yes_buy * 100:.0f}%" if result.yes_buy is not None else "unavailable"
            no = f"{result.no_buy * 100:.0f}%" if result.no_buy is not None else "unavailable"
            lines.append(
                f"[{_safe(result.market.question, limit=1000)}]({result.market.url})\n"
                f"· YES {yes} · NO {no} · {_volume_millions(result.market.volume_all_time)} · "
                f"ID `{result.market.id}`"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"Bets {first}–{last} of {len(event.markets)} · Page {page}/{page_count} · Select a bet below to place it"
        )
        return embed

    class BetStakeModal(discord.ui.Modal):
        def __init__(
            self, *, guild_id: str, channel_id: str, market_id: str, outcome: str
        ) -> None:
            super().__init__(title=f"Bet {outcome.upper()}")
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.market_id = market_id
            self.outcome = outcome
            self.stake = discord.ui.TextInput(
                label="Stake in credits",
                placeholder="For example, 25 or 12.50",
                max_length=20,
            )
            self.add_item(self.stake)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=False, thinking=True)
            await show_bet_confirmation(
                interaction,
                guild_id=self.guild_id,
                user_id=str(interaction.user.id),
                channel_id=self.channel_id,
                market_id=self.market_id,
                outcome=self.outcome,
                stake_value=self.stake.value,
            )

    class EventPageView(discord.ui.View):
        def __init__(self, event, page: int, page_count: int) -> None:
            super().__init__(timeout=300)
            self.event_id = event.id
            self.selected_market_id: str | None = None
            ranked_markets = sorted(
                event.markets,
                key=lambda market: market.outcome_prices.get("yes", Decimal("0")),
                reverse=True,
            )
            page_markets = ranked_markets[(page - 1) * 15 : page * 15]
            options = [
                discord.SelectOption(
                    label=(market.option_label or _option_label(market.question))[:100],
                    description=market.question[:100],
                    value=market.id,
                )
                for market in page_markets
            ]
            market_select = discord.ui.Select(
                placeholder="Choose a bet to place", options=options, row=0
            )

            yes_button = discord.ui.Button(
                label="Bet YES", style=discord.ButtonStyle.success, disabled=True, row=1
            )
            no_button = discord.ui.Button(
                label="Bet NO", style=discord.ButtonStyle.danger, disabled=True, row=1
            )

            async def select_market(component: discord.Interaction) -> None:
                self.selected_market_id = market_select.values[0]
                selected_label = next(
                    option.label
                    for option in market_select.options
                    if option.value == self.selected_market_id
                )
                for option in market_select.options:
                    option.default = option.value == self.selected_market_id
                market_select.placeholder = f"Selected: {selected_label}"[:150]
                yes_button.disabled = False
                no_button.disabled = False
                await component.response.edit_message(view=self)

            async def open_bet_modal(
                component: discord.Interaction, outcome: str
            ) -> None:
                if self.selected_market_id is None:
                    await component.response.send_message(
                        embed=_notice_embed("Choose a bet from the dropdown first."),
                        ephemeral=True,
                    )
                    return
                await component.response.send_modal(
                    BetStakeModal(
                        guild_id=str(component.guild_id),
                        channel_id=str(component.channel_id),
                        market_id=self.selected_market_id,
                        outcome=outcome,
                    )
                )

            async def bet_yes(component: discord.Interaction) -> None:
                await open_bet_modal(component, "yes")

            async def bet_no(component: discord.Interaction) -> None:
                await open_bet_modal(component, "no")

            market_select.callback = select_market
            yes_button.callback = bet_yes
            no_button.callback = bet_no
            self.add_item(market_select)
            self.add_item(yes_button)
            self.add_item(no_button)

            if page > 1:
                previous = discord.ui.Button(
                    label="Previous", style=discord.ButtonStyle.secondary, row=2
                )

                async def go_previous(component: discord.Interaction) -> None:
                    await self._show_page(component, page - 1)

                previous.callback = go_previous
                self.add_item(previous)
            if page < page_count:
                next_page = discord.ui.Button(
                    label="Next", style=discord.ButtonStyle.primary, row=2
                )

                async def go_next(component: discord.Interaction) -> None:
                    await self._show_page(component, page + 1)

                next_page.callback = go_next
                self.add_item(next_page)

        async def _show_page(self, interaction: discord.Interaction, page: int) -> None:
            await interaction.response.defer()
            try:
                event, results = await service.event_markets(
                    self.event_id, offset=(page - 1) * 15, limit=15
                )
            except MarketDataError:
                logger.exception("Could not load event page for %s", self.event_id)
                embed = discord.Embed(
                    title="Could not load bets",
                    description="Polymarket data is temporarily unavailable. Try again shortly.",
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return
            page_count = max(1, (len(event.markets) + 14) // 15)
            await interaction.edit_original_response(
                embed=event_embed(event, results, page),
                view=EventPageView(event, page, page_count),
            )

    def market_results_embed(events, page: int, query: str | None) -> discord.Embed:
        page_size = 15
        page_count = max(1, (len(events) + page_size - 1) // page_size)
        start = (page - 1) * page_size
        page_events = events[start : start + page_size]
        lines = []
        for event in page_events:
            leader = max(
                event.markets,
                key=lambda market: market.outcome_prices.get("yes", Decimal("0")),
            )
            probability = leader.outcome_prices.get("yes")
            option = leader.option_label or _option_label(leader.question)
            popular = (
                f"{_safe(option, limit=70)} · {probability * 100:.0f}%"
                if probability is not None
                else f"{_safe(option, limit=70)} · unavailable"
            )
            lines.append(
                f"[{_safe(event.title, limit=70)}]({event.url}) · "
                f"{_volume_millions(event.volume_all_time)} · {popular}"
            )
        title = (
            f"Events matching “{_safe(query.strip(), limit=100)}”"
            if query
            else "Top trending events · 24h volume"
        )
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
        )
        embed.set_footer(
            text=f"Page {page} of {page_count} · Select an event for its bets and details"
        )
        return embed

    def ending_soon_results_embed(markets, page: int, query: str | None) -> discord.Embed:
        page_size = 15
        page_count = max(1, (len(markets) + page_size - 1) // page_size)
        page_markets = markets[(page - 1) * page_size : page * page_size]
        lines = []
        for market in page_markets:
            end_timestamp = int(market.end_date.timestamp()) if market.end_date else 0
            lines.append(
                f"[{_safe(market.question, limit=150)}]({market.url}) · "
                f"{_volume_millions(market.volume_all_time)} all-time · "
                f"Ends <t:{end_timestamp}:R>"
            )
        title = "Bets ending in the next 7 days · market all-time volume"
        if query:
            title += f" · matching “{_safe(query.strip(), limit=80)}”"
        embed = discord.Embed(title=title, description="\n".join(lines))
        embed.set_footer(
            text=f"Page {page} of {page_count} · Select a bet below for its event details"
        )
        return embed

    class MarketsView(discord.ui.View):
        def __init__(self, events, page: int, query: str | None) -> None:
            super().__init__(timeout=300)
            self.events = events
            self.page = page
            self.query = query
            page_events = events[(page - 1) * 15 : page * 15]
            if page_events:
                options = [
                    discord.SelectOption(
                        label=event.title[:100],
                        description=f"{_volume_millions(event.volume_all_time)} · {len(event.markets)} bets"[:100],
                        value=event.id,
                    )
                    for event in page_events
                ]
                event_select = discord.ui.Select(
                    placeholder="Select an event to see its bets",
                    options=options,
                    row=0,
                )

                async def select_event(component: discord.Interaction) -> None:
                    selected_id = event_select.values[0]
                    await component.response.defer()
                    try:
                        event, results = await service.event_markets(
                            selected_id, offset=0, limit=15
                        )
                    except MarketDataError:
                        logger.exception("Could not load event %s", selected_id)
                        error_embed = discord.Embed(
                            title="Could not load bets",
                            description="Polymarket data is temporarily unavailable. Try again shortly.",
                        )
                        await component.edit_original_response(embed=error_embed, view=None)
                        return
                    page_count = max(1, (len(event.markets) + 14) // 15)
                    await component.edit_original_response(
                        embed=event_embed(event, results, 1),
                        view=EventPageView(event, 1, page_count),
                    )

                event_select.callback = select_event
                self.add_item(event_select)

            page_count = max(1, (len(events) + 14) // 15)
            if page > 1:
                previous = discord.ui.Button(
                    label="Previous", style=discord.ButtonStyle.secondary, row=1
                )

                async def go_previous(component: discord.Interaction) -> None:
                    await component.response.defer()
                    await component.edit_original_response(
                        embed=market_results_embed(
                            events, page - 1, query
                        ),
                        view=MarketsView(events, page - 1, query),
                    )

                previous.callback = go_previous
                self.add_item(previous)
            if page < page_count:
                next_page = discord.ui.Button(
                    label="Next", style=discord.ButtonStyle.primary, row=1
                )

                async def go_next(component: discord.Interaction) -> None:
                    await component.response.defer()
                    await component.edit_original_response(
                        embed=market_results_embed(
                            events, page + 1, query
                        ),
                        view=MarketsView(events, page + 1, query),
                    )

                next_page.callback = go_next
                self.add_item(next_page)

    class EndingSoonView(discord.ui.View):
        def __init__(self, markets, page: int, query: str | None) -> None:
            super().__init__(timeout=300)
            page_markets = markets[(page - 1) * 15 : page * 15]
            if page_markets:
                options = [
                    discord.SelectOption(
                        label=(market.option_label or market.question)[:100],
                        description=(
                            f"{_safe(market.event_title or 'Event', limit=45)} · "
                            f"{_volume_millions(market.volume_all_time)} all-time"
                        )[:100],
                        value=market.id,
                    )
                    for market in page_markets
                ]
                bet_select = discord.ui.Select(
                    placeholder="Select a bet to see its event details",
                    options=options,
                    row=0,
                )

                async def select_bet(component: discord.Interaction) -> None:
                    selected_market = next(
                        market for market in page_markets if market.id == bet_select.values[0]
                    )
                    if not selected_market.event_id:
                        await component.response.send_message(
                            embed=_notice_embed("Event details are unavailable for this bet."),
                            ephemeral=True,
                        )
                        return
                    await component.response.defer()
                    try:
                        event, results = await service.event_markets(
                            selected_market.event_id, offset=0, limit=15
                        )
                    except MarketDataError:
                        logger.exception(
                            "Could not load event %s", selected_market.event_id
                        )
                        error_embed = discord.Embed(
                            title="Could not load bets",
                            description="Polymarket data is temporarily unavailable. Try again shortly.",
                        )
                        await component.edit_original_response(embed=error_embed, view=None)
                        return
                    page_count = max(1, (len(event.markets) + 14) // 15)
                    await component.edit_original_response(
                        embed=event_embed(event, results, 1),
                        view=EventPageView(event, 1, page_count),
                    )

                bet_select.callback = select_bet
                self.add_item(bet_select)

            page_count = max(1, (len(markets) + 14) // 15)
            if page > 1:
                previous = discord.ui.Button(
                    label="Previous", style=discord.ButtonStyle.secondary, row=1
                )

                async def go_previous(component: discord.Interaction) -> None:
                    await component.response.defer()
                    await component.edit_original_response(
                        embed=ending_soon_results_embed(markets, page - 1, query),
                        view=EndingSoonView(markets, page - 1, query),
                    )

                previous.callback = go_previous
                self.add_item(previous)
            if page < page_count:
                next_page = discord.ui.Button(
                    label="Next", style=discord.ButtonStyle.primary, row=1
                )

                async def go_next(component: discord.Interaction) -> None:
                    await component.response.defer()
                    await component.edit_original_response(
                        embed=ending_soon_results_embed(markets, page + 1, query),
                        view=EndingSoonView(markets, page + 1, query),
                    )

                next_page.callback = go_next
                self.add_item(next_page)

    @bot.tree.command(name="markets", description="Browse Polymarket events")
    @app_commands.describe(
        query="Optional event name or keyword to search",
        sort_by="Choose trending events or events ending soon",
    )
    @app_commands.choices(
        sort_by=[
            app_commands.Choice(name="Trending · 24h volume", value="trending"),
            app_commands.Choice(name="Ending soon · next 7 days", value="ending_soon"),
        ]
    )
    async def markets_command(
        interaction: discord.Interaction,
        query: str | None = None,
        sort_by: app_commands.Choice[str] | None = None,
    ) -> None:
        # Acknowledge immediately: Polymarket search can take longer than
        # Discord's three-second interaction response window.
        await interaction.response.defer(ephemeral=False, thinking=True)
        if interaction.guild_id is None:
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed("Use this command in a Discord server."),
            )
            return
        ending_soon = bool(sort_by and sort_by.value == "ending_soon")
        try:
            if ending_soon:
                query = query.strip() if query and query.strip() else None
                results = await service.ending_soon_markets(
                    limit=100, days=7, query=query
                )
            elif query and query.strip():
                query = query.strip()
                results = await service.search_events(query, limit=100)
            else:
                query = None
                results = await service.trending_events(limit=100)
        except MarketDataError:
            logger.exception("Market event lookup failed")
            embed = discord.Embed(
                title="Event list unavailable",
                description="Polymarket data is temporarily unavailable. Try again shortly.",
            )
            await interaction.edit_original_response(content=None, embed=embed)
            return
        if not results:
            embed = discord.Embed(
                title=(
                    "No matching bets ending soon"
                    if ending_soon and query
                    else "No bets ending soon"
                    if ending_soon
                    else "No matching events"
                    if query
                    else "No trending events"
                ),
                description=(
                    "Try a shorter keyword or a different part of the event name."
                    if query
                    else "There are no active supported events to show right now."
                ),
            )
            await interaction.edit_original_response(content=None, embed=embed)
            return

        if ending_soon:
            await interaction.edit_original_response(
                content=None,
                embed=ending_soon_results_embed(results, 1, query),
                view=EndingSoonView(results, 1, query),
            )
        else:
            await interaction.edit_original_response(
                content=None,
                embed=market_results_embed(results, 1, query),
                view=MarketsView(results, 1, query),
            )

    @bot.tree.command(name="event", description="Show the YES/NO bets inside a Polymarket event")
    @app_commands.describe(
        event_id="Event ID shown by /markets",
        page="Which page of bets to show (15 bets per page)",
    )
    async def event_command(
        interaction: discord.Interaction, event_id: str, page: app_commands.Range[int, 1, 100] = 1
    ) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            event, results = await service.event_markets(
                event_id.strip(), offset=(page - 1) * 15, limit=15
            )
        except MarketDataError:
            logger.exception("Could not load event %s", event_id)
            embed = discord.Embed(
                title="Could not load event",
                description="Check the event ID, or try searching again with `/markets`.",
            )
            await interaction.edit_original_response(content=None, embed=embed)
            return

        page_count = max(1, (len(event.markets) + 14) // 15)
        if page > page_count:
            embed = discord.Embed(
                title=_safe(event.title, limit=250),
                url=event.url,
                description=f"This event has {len(event.markets)} bets. Choose a page from 1 to {page_count}.",
            )
            await interaction.edit_original_response(content=None, embed=embed)
            return

        await interaction.edit_original_response(
            content=None,
            embed=event_embed(event, results, page),
            view=EventPageView(event, page, page_count),
        )

    class BetConfirmationView(discord.ui.View):
        def __init__(
            self,
            *,
            user_id: str,
            guild_id: str,
            market_id: str,
            outcome: str,
            stake_value: str,
            channel_id: str,
        ) -> None:
            super().__init__(timeout=120)
            self.user_id = user_id
            self.guild_id = guild_id
            self.market_id = market_id
            self.outcome = outcome
            self.stake_value = stake_value
            self.channel_id = channel_id
            self.used = False
            self.message: discord.Message | None = None

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if str(interaction.user.id) != self.user_id:
                await interaction.response.send_message(
                    embed=_notice_embed(
                        "Only the member who started this bet can confirm or cancel it."
                    ),
                    ephemeral=True,
                )
                return False
            return True

        async def on_timeout(self) -> None:
            for item in self.children:
                item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    logger.debug("Could not disable expired bet confirmation controls")

        @discord.ui.button(label="Confirm bet", style=discord.ButtonStyle.success)
        async def confirm_bet(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self.used:
                await interaction.response.send_message(
                    embed=_notice_embed("This bet confirmation has already been used."),
                    ephemeral=True,
                )
                return
            self.used = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer()
            try:
                position = await service.place_bet(
                    guild_id=self.guild_id,
                    user_id=self.user_id,
                    market_id=self.market_id,
                    channel_id=self.channel_id,
                    outcome=self.outcome,
                    stake_value=self.stake_value,
                )
            except (InsufficientFunds, InvalidStake) as exc:
                await interaction.edit_original_response(
                    content=None,
                    embed=_notice_embed(
                        str(exc), title="Could not place bet", color=discord.Color.red()
                    ),
                    view=None,
                )
                return
            except MarketDataError:
                logger.exception("Could not confirm simulated bet")
                await interaction.edit_original_response(
                    content=None,
                    embed=_notice_embed(
                        "The market or price may have changed. Try again.",
                        title="Could not place bet",
                        color=discord.Color.red(),
                    ),
                    view=None,
                )
                return

            account = service.account(self.guild_id, self.user_id)
            result_embed = discord.Embed(
                title="Bet placed",
                description=(
                    f"<@{self.user_id}> confirmed **{position.outcome.upper()}** on "
                    f"[{_safe(position.question)}]({position.market_url})"
                ),
                color=discord.Color.green(),
            )
            result_embed.add_field(
                name="Stake", value=f"{_credits(position.stake)} credits"
            )
            result_embed.add_field(
                name="Buy price", value=f"{position.entry_price * 100:.2f}%"
            )
            result_embed.add_field(name="Shares", value=f"{position.shares:.4f}")
            result_embed.add_field(
                name="If correct",
                value=(
                    f"{_credits(position.shares)} credits payout · "
                    f"{_credits(position.shares - position.stake)} profit"
                ),
                inline=False,
            )
            result_embed.add_field(
                name="Available balance",
                value=f"{_credits(account.balance)} credits",
            )
            await interaction.edit_original_response(
                content=None,
                embed=result_embed,
                view=None,
            )

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel_bet(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self.used:
                await interaction.response.send_message(
                    embed=_notice_embed("This bet confirmation has already been used."),
                    ephemeral=True,
                )
                return
            self.used = True
            await interaction.response.edit_message(
                content=None,
                embed=_notice_embed("Bet cancelled. No credits were spent."),
                view=None,
            )

    async def show_bet_confirmation(
        interaction: discord.Interaction,
        *,
        guild_id: str,
        user_id: str,
        channel_id: str,
        market_id: str,
        outcome: str,
        stake_value: str,
    ) -> None:
        try:
            market, normalized, stake_amount, price = await service.quote_bet(
                market_id=market_id,
                outcome=outcome,
                stake_value=stake_value,
            )
            account = service.account(guild_id, user_id)
            if stake_amount > account.balance:
                raise InsufficientFunds(
                    f"Available balance is {_credits(account.balance)} credits."
                )
        except (InsufficientFunds, InvalidStake) as exc:
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    str(exc), title="Could not prepare bet", color=discord.Color.red()
                ),
            )
            return
        except MarketDataError:
            logger.exception("Could not prepare simulated bet confirmation")
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    "Check the market ID and current price, then try again.",
                    title="Could not prepare bet",
                    color=discord.Color.red(),
                ),
            )
            return

        embed = discord.Embed(
            title="Confirm simulated bet",
            description=(
                f"**{normalized.upper()}** on [{_safe(market.question)}]({market.url})"
            ),
        )
        embed.add_field(name="Stake", value=f"{_credits(stake_amount)} credits")
        embed.add_field(name="Current buy price", value=f"{price * 100:.2f}%")
        embed.add_field(name="Estimated shares", value=f"{stake_amount / price:.4f}")
        estimated_payout = stake_amount / price
        embed.add_field(
            name="If correct",
            value=(
                f"{_credits(estimated_payout)} credits payout · "
                f"{_credits(estimated_payout - stake_amount)} profit"
            ),
            inline=False,
        )
        embed.add_field(
            name="Balance after confirmation",
            value=f"{_credits(account.balance - stake_amount)} credits",
        )
        embed.set_footer(
            text="The price is checked again when you confirm; no credits are spent yet."
        )
        view = BetConfirmationView(
            user_id=user_id,
            guild_id=guild_id,
            market_id=market_id,
            outcome=normalized,
            stake_value=stake_value,
            channel_id=channel_id,
        )
        view.message = await interaction.edit_original_response(
            content=None, embed=embed, view=view
        )

    @bot.tree.command(name="bet", description="Place a simulated YES or NO bet")
    @app_commands.describe(
        market_id="Market ID shown by /markets",
        outcome="Choose YES or NO",
        stake="Play credits to spend, for example 25 or 12.50",
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="YES", value="yes"),
            app_commands.Choice(name="NO", value="no"),
        ]
    )
    async def bet_command(
        interaction: discord.Interaction,
        market_id: str,
        outcome: app_commands.Choice[str],
        stake: str,
    ) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        await show_bet_confirmation(
            interaction,
            guild_id=guild_id,
            user_id=str(interaction.user.id),
            channel_id=str(interaction.channel_id),
            market_id=market_id,
            outcome=outcome.value,
            stake_value=stake,
        )

    class SellAmountModal(discord.ui.Modal):
        def __init__(self, *, guild_id: str, user_id: str, position_id: int) -> None:
            super().__init__(title="Sell position shares")
            self.guild_id = guild_id
            self.user_id = user_id
            self.position_id = position_id
            self.amount = discord.ui.TextInput(
                label="Shares, percentage, or all",
                placeholder="For example: 250, 25%, or all",
                max_length=32,
            )
            self.add_item(self.amount)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=False, thinking=True)
            await show_sale_confirmation(
                interaction,
                guild_id=self.guild_id,
                user_id=self.user_id,
                position_id=self.position_id,
                amount_value=self.amount.value,
            )

    class SellConfirmationView(discord.ui.View):
        def __init__(
            self, *, guild_id: str, user_id: str, position_id: int, shares_to_sell: Decimal
        ) -> None:
            super().__init__(timeout=120)
            self.guild_id = guild_id
            self.user_id = user_id
            self.position_id = position_id
            self.shares_to_sell = shares_to_sell
            self.used = False
            self.message: discord.Message | None = None

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if str(interaction.user.id) != self.user_id:
                await interaction.response.send_message(
                    embed=_notice_embed("Only the member who owns this position can sell it."),
                    ephemeral=True,
                )
                return False
            return True

        async def on_timeout(self) -> None:
            for item in self.children:
                item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    logger.debug("Could not disable expired sale confirmation controls")

        @discord.ui.button(label="Confirm sale", style=discord.ButtonStyle.danger)
        async def confirm_sale(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self.used:
                await interaction.response.send_message(
                    embed=_notice_embed("This sale confirmation has already been used."),
                    ephemeral=True,
                )
                return
            self.used = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer()
            try:
                result = await service.sell_position(
                    guild_id=self.guild_id,
                    user_id=self.user_id,
                    position_id=self.position_id,
                    shares_to_sell=self.shares_to_sell,
                )
            except InvalidStake as exc:
                await interaction.edit_original_response(
                    content=None,
                    embed=_notice_embed(
                        str(exc), title="Could not sell position", color=discord.Color.red()
                    ),
                    view=None,
                )
                return
            except MarketDataError:
                logger.exception("Could not confirm simulated sale")
                await interaction.edit_original_response(
                    content=None,
                    embed=_notice_embed(
                        "The current sell price is unavailable. No credits were changed; try again.",
                        title="Could not sell position",
                        color=discord.Color.red(),
                    ),
                    view=None,
                )
                return

            position = result.position
            profit = result.proceeds - result.cost_basis
            sign = "+" if profit > 0 else ""
            account = service.account(self.guild_id, self.user_id)
            embed = discord.Embed(
                title="Position partially sold" if position.remaining_shares else "Position sold",
                description=(
                    f"<@{self.user_id}> sold {result.shares_sold:.4f} shares from position `#{position.id}`: "
                    f"**{position.outcome.upper()}** "
                    f"on [{_safe(position.question)}]({position.market_url})"
                ),
                color=discord.Color.green(),
            )
            embed.add_field(name="Shares sold", value=f"{result.shares_sold:.4f}")
            embed.add_field(
                name="Sell price", value=f"{result.price * 100:.2f}%"
            )
            embed.add_field(
                name="Original buy price", value=f"{position.entry_price * 100:.2f}%"
            )
            embed.add_field(name="Original bet", value=f"{_credits(position.stake)} credits")
            embed.add_field(name="Proceeds", value=f"{_credits(result.proceeds)} credits")
            embed.add_field(name="Realized P/L", value=f"{sign}{_credits(profit)} credits")
            if position.remaining_shares:
                embed.add_field(
                    name="Shares remaining", value=f"{position.remaining_shares:.4f}"
                )
            embed.add_field(
                name="Available balance", value=f"{_credits(account.balance)} credits"
            )
            await interaction.edit_original_response(content=None, embed=embed, view=None)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel_sale(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self.used:
                await interaction.response.send_message(
                    embed=_notice_embed("This sale confirmation has already been used."),
                    ephemeral=True,
                )
                return
            self.used = True
            await interaction.response.edit_message(
                content=None,
                embed=_notice_embed("Sale cancelled. Your position is still open."),
                view=None,
            )

    async def show_sale_confirmation(
        interaction: discord.Interaction,
        *,
        guild_id: str,
        user_id: str,
        position_id: int,
        amount_value: str,
    ) -> None:
        try:
            quote = await service.quote_sale(
                guild_id=guild_id,
                user_id=user_id,
                position_id=position_id,
                amount_value=amount_value,
            )
        except InvalidStake as exc:
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    str(exc), title="Could not prepare sale", color=discord.Color.red()
                ),
            )
            return
        except MarketDataError:
            logger.exception("Could not prepare simulated sale confirmation")
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    "The current sell price is unavailable. Try again shortly.",
                    title="Could not prepare sale",
                    color=discord.Color.red(),
                ),
            )
            return

        position = quote.position
        profit = quote.proceeds - quote.cost_basis
        sign = "+" if profit > 0 else ""
        embed = discord.Embed(
            title="Confirm sale",
            description=(
                f"Sell **{quote.shares_to_sell:.4f} shares** of position `#{position.id}`: "
                f"**{position.outcome.upper()}** on "
                f"[{_safe(position.question)}]({position.market_url})?"
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="Shares to sell", value=f"{quote.shares_to_sell:.4f}")
        embed.add_field(
            name="Entry → sell bid",
            value=f"{position.entry_price * 100:.2f}% → {quote.price * 100:.2f}%",
        )
        embed.add_field(name="Estimated proceeds", value=f"{_credits(quote.proceeds)} credits")
        embed.add_field(
            name="Estimated P/L", value=f"{sign}{_credits(profit)} credits"
        )
        embed.add_field(
            name="Shares remaining", value=f"{quote.remaining_shares:.4f}"
        )
        embed.set_footer(
            text="The price is checked again when you confirm. No credits change until then."
        )
        view = SellConfirmationView(
            guild_id=guild_id,
            user_id=user_id,
            position_id=position_id,
            shares_to_sell=quote.shares_to_sell,
        )
        view.message = await interaction.edit_original_response(
            content=None, embed=embed, view=view
        )

    class OpenPositionSellView(discord.ui.View):
        def __init__(self, *, guild_id: str, user_id: str, positions: list[Position]) -> None:
            super().__init__(timeout=300)
            self.guild_id = guild_id
            self.user_id = user_id
            options = [
                discord.SelectOption(
                    label=f"#{position.id} · {position.outcome.upper()} · {_safe(position.question, limit=55)}"[:100],
                    description=(
                        f"{position.remaining_shares:.4f} shares left · "
                        f"bet {_credits(position.stake)} credits"
                    )[:100],
                    value=str(position.id),
                )
                for position in positions[:25]
            ]
            position_select = discord.ui.Select(
                placeholder="Choose an open position to sell",
                options=options,
                row=0,
            )

            async def select_position(component: discord.Interaction) -> None:
                await component.response.send_modal(
                    SellAmountModal(
                        guild_id=self.guild_id,
                        user_id=self.user_id,
                        position_id=int(position_select.values[0]),
                    )
                )

            position_select.callback = select_position
            self.add_item(position_select)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if str(interaction.user.id) != self.user_id:
                await interaction.response.send_message(
                    embed=_notice_embed("Only the member who owns these positions can sell them."),
                    ephemeral=True,
                )
                return False
            return True

    @bot.tree.command(name="balance", description="Show a member's public play-credit balance")
    @app_commands.describe(member="Member to inspect; defaults to you")
    async def balance_command(
        interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        selected = member or interaction.user
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            portfolio = await service.portfolio(guild_id, str(selected.id))
        except MarketDataError:
            logger.exception("Portfolio quote lookup failed")
            balance = service.account(guild_id, str(selected.id)).balance
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    f"{selected.mention} has {_credits(balance)} available credits. "
                    "Open-position value is temporarily unavailable.",
                    title=f"{_safe(selected.display_name)}'s balance",
                ),
            )
            return
        note = (
            f"\n{portfolio.unpriced_positions} open position(s) have no current sell quote."
            if portfolio.unpriced_positions
            else ""
        )
        balance_embed = discord.Embed(
            title=f"{_safe(selected.display_name)}'s balance",
            color=discord.Color.blurple(),
        )
        balance_embed.add_field(
            name="Available credits", value=_credits(portfolio.account.balance)
        )
        balance_embed.add_field(
            name="Open positions", value=_credits(portfolio.position_value)
        )
        balance_embed.add_field(
            name="Portfolio total", value=f"{_credits(portfolio.total_value)} credits"
        )
        if note:
            balance_embed.set_footer(text=note.strip())
        await interaction.edit_original_response(content=None, embed=balance_embed)

    @bot.tree.command(name="positions", description="Show a member's public simulated positions")
    @app_commands.describe(member="Member to inspect; defaults to you")
    async def positions_command(
        interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        selected = member or interaction.user
        positions = service.positions(guild_id, str(selected.id))
        if not positions:
            embed = discord.Embed(
                title=f"{_safe(selected.display_name)}'s positions",
                description=f"{selected.mention} has no positions yet.",
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=False)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        current_values = await service.position_values(positions)
        open_positions = [
            position
            for position in service.store.open_positions(guild_id)
            if position.user_id == str(selected.id)
        ]
        lines = [
            _history_line(position, current_values.get(position.id))
            for position in positions
        ]

        description = ""
        for index, line in enumerate(lines):
            candidate = f"{description}\n{line}" if description else line
            remaining_count = len(lines) - index - 1
            omitted_marker = (
                f"\n… and {remaining_count} more positions" if remaining_count else ""
            )
            if len(candidate) + len(omitted_marker) > 4096:
                marker = f"… and {len(lines) - index} more positions"
                if len(description) + len(marker) + 1 <= 4096:
                    description = f"{description}\n{marker}" if description else marker
                break
            description = candidate

        embed = discord.Embed(
            title=f"{_safe(selected.display_name)}'s positions",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                "Showing up to 15 most recent bets · P/L is versus stake."
            )
        )
        view = None
        if open_positions and str(interaction.user.id) == str(selected.id):
            embed.set_footer(
                text=(
                    "Showing up to 15 most recent bets · Select from up to 25 most recent open "
                    "positions below to sell. Open values use current quotes; P/L is versus stake."
                )
            )
            view = OpenPositionSellView(
                guild_id=guild_id,
                user_id=str(selected.id),
                positions=open_positions,
            )
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    @bot.tree.command(name="history", description="Show recent simulated bets")
    @app_commands.describe(
        member="Member to inspect; defaults to you",
        limit="How many recent bets to show (1–15)",
    )
    async def history_command(
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 15] = 10,
    ) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        selected = member or interaction.user
        history = service.bet_history(guild_id, str(selected.id), limit)
        embed = discord.Embed(
            title=f"{_safe(selected.display_name)}'s recent bets",
            color=discord.Color.blurple(),
        )
        if not history:
            embed.description = "No bets found. Place a bet with `/markets` to get started."
        else:
            await interaction.response.defer(ephemeral=False, thinking=True)
            current_values = await service.position_values(history)
            lines = [
                _history_line(position, current_values.get(position.id))
                for position in history
            ]
            embed.description = "\n\n".join(lines)
        embed.set_footer(
            text=(
                f"Showing up to {limit} most recent bets · "
                "Open values use current sell quotes; P/L is versus stake."
            )
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=False)

    @bot.tree.command(name="leaderboard", description="Show server portfolio rankings")
    async def leaderboard_command(interaction: discord.Interaction) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            ranked = await service.leaderboard(guild_id)
        except MarketDataError:
            logger.exception("Leaderboard quote lookup failed")
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    "Portfolio values are temporarily unavailable; try again shortly.",
                    title="Leaderboard unavailable",
                    color=discord.Color.red(),
                ),
            )
            return
        if not ranked:
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    "No players yet. Place a bet to join.", title="Leaderboard"
                ),
            )
            return
        lines = []
        for rank, (account, total, unpriced) in enumerate(ranked[:10], start=1):
            member = None
            if interaction.guild:
                user_id = int(account.user_id)
                member = interaction.guild.get_member(user_id)
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(user_id)
                    except discord.NotFound:
                        pass  # Keep old accounts visible after a member leaves.
                    except discord.HTTPException:
                        logger.exception("Could not fetch leaderboard member %s", user_id)
            # Discord renders this mention as the account name even if the member
            # was not present in the local cache or could not be fetched.
            name = _safe(member.display_name) if member else f"<@{account.user_id}>"
            suffix = f" ({unpriced} unpriced)" if unpriced else ""
            lines.append(f"**{rank}. {name}** — {_credits(total)} credits{suffix}")
        await interaction.edit_original_response(
            content=None,
            embed=_notice_embed("\n".join(lines), title="Server leaderboard"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    admin_group = app_commands.Group(name="admin", description="Server play-credit administration")

    @admin_group.command(name="reset", description="Reset a member or the server to 10,000 credits")
    @app_commands.describe(member="Member to reset; omit to reset every existing account")
    async def reset_command(
        interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.manage_guild:
            await interaction.response.send_message(
                embed=_notice_embed(
                    "You need Manage Server permission to reset play credits.",
                    title="Permission required",
                    color=discord.Color.red(),
                ),
                ephemeral=False,
            )
            return
        accounts, voided = service.reset(guild_id, str(member.id) if member else None)
        who = member.mention if member else "all existing server accounts"
        await interaction.response.send_message(
            embed=_notice_embed(
                f"Reset {who} to 10,000 credits. Voided {voided} open position(s); "
                f"accounts reset: {accounts}.",
                title="Credits reset",
                color=discord.Color.green(),
            ),
            ephemeral=False,
        )

    @admin_group.command(
        name="test-settlement", description="Post a sample bet-resolution notification here"
    )
    async def test_settlement_command(interaction: discord.Interaction) -> None:
        guild_id = await ensure_public_context(interaction)
        if guild_id is None:
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.manage_guild:
            await interaction.response.send_message(
                embed=_notice_embed(
                    "You need Manage Server permission to post a test settlement notice.",
                    title="Permission required",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        if interaction.channel is None:
            await interaction.response.send_message(
                embed=_notice_embed(
                    "I could not find this channel.", color=discord.Color.red()
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content, embed = _settlement_notice(
                str(interaction.user.id),
                "YES",
                "PolySim settlement notification demo (not a real market)",
                "https://polymarket.com",
                Decimal("1"),
                Decimal("1000"),
                Decimal("1250"),
            )
            await interaction.channel.send(
                content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[interaction.user], roles=False, everyone=False
                ),
            )
        except discord.HTTPException:
            logger.exception("Could not post test settlement notice")
            await interaction.edit_original_response(
                content=None,
                embed=_notice_embed(
                    "I could not post the notice. Check that I can send messages in this channel.",
                    title="Could not post notice",
                    color=discord.Color.red(),
                ),
            )
            return
        await interaction.edit_original_response(
            content=None,
            embed=_notice_embed(
                "Posted a sample settlement notice here. This did not place a bet or change credits.",
                title="Test notice posted",
                color=discord.Color.green(),
            ),
        )

    bot.tree.add_command(admin_group)
    return bot


async def run(settings: Settings) -> None:
    bot = create_bot(settings)
    async with bot:
        await bot.start(settings.discord_token)
