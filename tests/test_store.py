from decimal import Decimal

import pytest

from polysim.store import STARTING_BALANCE, InsufficientFunds, InvalidStake, Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "polysim.sqlite3")
    instance.initialize()
    return instance


def place(store, *, guild="guild-1", user="user-1", outcome="yes", stake="100"):
    return store.place_bet(
        guild_id=guild,
        user_id=user,
        market_id="market-1",
        channel_id="channel-1",
        question="Will it happen?",
        market_url="https://polymarket.com/market/test-market",
        outcome=outcome,
        token_id=f"token-{outcome}",
        entry_price=Decimal("0.25"),
        stake=Decimal(stake),
    )


def test_new_account_has_1000_credits_and_is_server_scoped(store):
    first = store.account("guild-1", "user-1")
    other_server = store.account("guild-2", "user-1")

    assert first.balance == STARTING_BALANCE
    assert other_server.balance == STARTING_BALANCE
    assert store.leaderboard_accounts("guild-1") == [first]


def test_bet_debits_stake_and_records_shares(store):
    position = place(store)

    assert position.status == "open"
    assert position.shares == Decimal("400")
    assert position.channel_id == "channel-1"
    assert store.account("guild-1", "user-1").balance == Decimal("900")


def test_insufficient_or_invalid_stake_does_not_change_balance(store):
    with pytest.raises(InsufficientFunds):
        place(store, stake="1001")
    with pytest.raises(InvalidStake):
        place(store, stake="NaN")

    assert store.account("guild-1", "user-1").balance == STARTING_BALANCE
    assert store.open_positions("guild-1") == []


@pytest.mark.parametrize(
    ("outcome_values", "expected_payout", "expected_balance"),
    [
        ({"yes": Decimal("1"), "no": Decimal("0")}, Decimal("400"), Decimal("1300")),
        ({"yes": Decimal("0.5"), "no": Decimal("0.5")}, Decimal("200"), Decimal("1100")),
        ({"yes": Decimal("0"), "no": Decimal("1")}, Decimal("0"), Decimal("900")),
    ],
)
def test_settlement_pays_winning_share_value_once(
    store, outcome_values, expected_payout, expected_balance
):
    position = place(store)

    settled = store.settle_market("market-1", outcome_values)
    repeated = store.settle_market("market-1", outcome_values)

    assert len(settled) == 1
    assert settled[0].id == position.id
    assert settled[0].status == "settled"
    assert settled[0].payout == expected_payout
    assert repeated == []
    assert store.account("guild-1", "user-1").balance == expected_balance


def test_reset_restores_credits_and_voids_open_bets_but_keeps_history(store):
    position = place(store, stake="250")

    accounts, voided = store.reset("guild-1", "user-1")
    history = store.list_positions("guild-1", "user-1")

    assert (accounts, voided) == (1, 1)
    assert store.account("guild-1", "user-1").balance == STARTING_BALANCE
    assert [(item.id, item.status) for item in history] == [(position.id, "void")]
    assert store.settle_market("market-1", {"yes": Decimal("1"), "no": Decimal("0")}) == []


def test_server_reset_does_not_change_other_server(store):
    place(store, guild="guild-1", user="user-1", stake="100")
    place(store, guild="guild-2", user="user-1", stake="200")

    accounts, voided = store.reset("guild-1")

    assert (accounts, voided) == (1, 1)
    assert store.account("guild-1", "user-1").balance == STARTING_BALANCE
    assert store.account("guild-2", "user-1").balance == Decimal("800")
