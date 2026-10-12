"""SQLite persistence and transaction-safe simulated betting operations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

STARTING_BALANCE = Decimal("10000")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _amount(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class Position:
    id: int
    guild_id: str
    user_id: str
    market_id: str
    channel_id: str
    question: str
    market_url: str
    outcome: str
    token_id: str
    entry_price: Decimal
    shares: Decimal
    stake: Decimal
    status: str
    opened_at: str
    resolved_at: str | None
    payout: Decimal | None
    outcome_value: Decimal | None
    remaining_shares: Decimal
    remaining_stake: Decimal
    realized_proceeds: Decimal
    realized_cost_basis: Decimal
    last_activity_at: str


@dataclass(frozen=True)
class SaleResult:
    position: Position
    shares_sold: Decimal
    price: Decimal
    proceeds: Decimal
    cost_basis: Decimal


@dataclass(frozen=True)
class Account:
    guild_id: str
    user_id: str
    balance: Decimal


class InsufficientFunds(ValueError):
    pass


class InvalidStake(ValueError):
    pass


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    balance TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    market_url TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    shares TEXT NOT NULL,
                    stake TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    opened_at TEXT NOT NULL,
                    resolved_at TEXT,
                    payout TEXT,
                    outcome_value TEXT,
                    remaining_shares TEXT NOT NULL DEFAULT '0',
                    remaining_stake TEXT NOT NULL DEFAULT '0',
                    realized_proceeds TEXT NOT NULL DEFAULT '0',
                    realized_cost_basis TEXT NOT NULL DEFAULT '0',
                    last_activity_at TEXT,
                    FOREIGN KEY (guild_id, user_id) REFERENCES accounts(guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS positions_open_market
                    ON positions(guild_id, market_id, status);
                CREATE INDEX IF NOT EXISTS positions_user
                    ON positions(guild_id, user_id, opened_at DESC);
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (guild_id, user_id) REFERENCES accounts(guild_id, user_id)
                );
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(positions)").fetchall()
            }
            migrations = {
                "remaining_shares": "TEXT NOT NULL DEFAULT '0'",
                "remaining_stake": "TEXT NOT NULL DEFAULT '0'",
                "realized_proceeds": "TEXT NOT NULL DEFAULT '0'",
                "realized_cost_basis": "TEXT NOT NULL DEFAULT '0'",
                "last_activity_at": "TEXT",
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE positions ADD COLUMN {column} {declaration}")
            db.execute(
                "UPDATE positions SET remaining_shares=shares,remaining_stake=stake "
                "WHERE status='open' AND remaining_shares='0'"
            )
            db.execute(
                "UPDATE positions SET realized_proceeds=COALESCE(payout,'0'),"
                "realized_cost_basis=stake WHERE status='sold' AND realized_proceeds='0'"
            )
            db.execute(
                "UPDATE positions SET last_activity_at=COALESCE(resolved_at,opened_at) "
                "WHERE last_activity_at IS NULL"
            )

    def _ensure_account(self, db: sqlite3.Connection, guild_id: str, user_id: str) -> None:
        now = _now()
        cursor = db.execute(
            "INSERT OR IGNORE INTO accounts VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, _amount(STARTING_BALANCE), now, now),
        )
        if cursor.rowcount:
            db.execute(
                "INSERT INTO ledger(guild_id,user_id,kind,amount,description,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (guild_id, user_id, "grant", _amount(STARTING_BALANCE), "Starting credits", now),
            )

    @staticmethod
    def _account(db: sqlite3.Connection, guild_id: str, user_id: str) -> Account:
        row = db.execute(
            "SELECT guild_id,user_id,balance FROM accounts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        assert row is not None
        return Account(row["guild_id"], row["user_id"], Decimal(row["balance"]))

    @staticmethod
    def _position(row: sqlite3.Row) -> Position:
        return Position(
            id=row["id"],
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            market_id=row["market_id"],
            channel_id=row["channel_id"],
            question=row["question"],
            market_url=row["market_url"],
            outcome=row["outcome"],
            token_id=row["token_id"],
            entry_price=Decimal(row["entry_price"]),
            shares=Decimal(row["shares"]),
            stake=Decimal(row["stake"]),
            status=row["status"],
            opened_at=row["opened_at"],
            resolved_at=row["resolved_at"],
            payout=Decimal(row["payout"]) if row["payout"] is not None else None,
            outcome_value=(
                Decimal(row["outcome_value"]) if row["outcome_value"] is not None else None
            ),
            remaining_shares=Decimal(row["remaining_shares"]),
            remaining_stake=Decimal(row["remaining_stake"]),
            realized_proceeds=Decimal(row["realized_proceeds"]),
            realized_cost_basis=Decimal(row["realized_cost_basis"]),
            last_activity_at=row["last_activity_at"] or row["opened_at"],
        )

    def account(self, guild_id: str, user_id: str) -> Account:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_account(db, guild_id, user_id)
            account = self._account(db, guild_id, user_id)
            return account

    def place_bet(
        self,
        *,
        guild_id: str,
        user_id: str,
        market_id: str,
        channel_id: str,
        question: str,
        market_url: str,
        outcome: str,
        token_id: str,
        entry_price: Decimal,
        stake: Decimal,
    ) -> Position:
        try:
            stake = Decimal(stake)
        except (InvalidOperation, TypeError) as exc:
            raise InvalidStake("Stake must be a positive credit amount") from exc
        if not stake.is_finite() or stake <= 0:
            raise InvalidStake("Stake must be a positive credit amount")
        if not entry_price.is_finite() or entry_price <= 0 or entry_price > 1:
            raise InvalidStake("There is no usable BUY price for this outcome")

        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_account(db, guild_id, user_id)
            account = self._account(db, guild_id, user_id)
            if stake > account.balance:
                raise InsufficientFunds(f"Available balance is {account.balance} credits")
            shares = stake / entry_price
            now = _now()
            cursor = db.execute(
                """INSERT INTO positions(
                    guild_id,user_id,market_id,channel_id,question,market_url,outcome,token_id,
                    entry_price,shares,stake,status,opened_at,remaining_shares,remaining_stake,
                    last_activity_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open',?,?,?,?)""",
                (
                    guild_id,
                    user_id,
                    market_id,
                    channel_id,
                    question,
                    market_url,
                    outcome.lower(),
                    token_id,
                    _amount(entry_price),
                    _amount(shares),
                    _amount(stake),
                    now,
                    _amount(shares),
                    _amount(stake),
                    now,
                ),
            )
            db.execute(
                "UPDATE accounts SET balance=?,updated_at=? WHERE guild_id=? AND user_id=?",
                (_amount(account.balance - stake), now, guild_id, user_id),
            )
            db.execute(
                "INSERT INTO ledger(guild_id,user_id,kind,amount,description,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    guild_id,
                    user_id,
                    "bet",
                    _amount(-stake),
                    f"{outcome.upper()} on {question}",
                    now,
                ),
            )
            row = db.execute("SELECT * FROM positions WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._position(row)

    def list_positions(
        self, guild_id: str, user_id: str, limit: int = 15
    ) -> list[Position]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM positions WHERE guild_id=? AND user_id=? "
                "AND status IN ('open','settled','void') "
                "ORDER BY opened_at DESC,id DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()
            return [self._position(row) for row in rows]

    def bet_history(
        self, guild_id: str, user_id: str, limit: int = 10
    ) -> list[Position]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM positions WHERE guild_id=? AND user_id=? "
                "ORDER BY last_activity_at DESC,id DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()
            return [self._position(row) for row in rows]

    def open_position(self, guild_id: str, user_id: str, position_id: int) -> Position | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM positions WHERE id=? AND guild_id=? AND user_id=? "
                "AND status='open'",
                (position_id, guild_id, user_id),
            ).fetchone()
            return self._position(row) if row is not None else None

    def sell_position(
        self,
        *,
        guild_id: str,
        user_id: str,
        position_id: int,
        shares_to_sell: Decimal,
        price: Decimal,
    ) -> SaleResult:
        if not price.is_finite() or not Decimal("0") < price <= Decimal("1"):
            raise InvalidStake("There is no usable SELL price for this position")
        try:
            shares_to_sell = Decimal(shares_to_sell)
        except (InvalidOperation, TypeError) as exc:
            raise InvalidStake("Enter a valid number of shares to sell") from exc
        if not shares_to_sell.is_finite() or shares_to_sell <= 0:
            raise InvalidStake("Shares to sell must be greater than zero")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM positions WHERE id=? AND guild_id=? AND user_id=? "
                "AND status='open'",
                (position_id, guild_id, user_id),
            ).fetchone()
            if row is None:
                raise InvalidStake("That position is no longer open or does not belong to you")
            position = self._position(row)
            if shares_to_sell > position.remaining_shares:
                raise InvalidStake(
                    f"You can sell up to {position.remaining_shares:.4f} remaining shares"
                )
            is_final_sale = shares_to_sell == position.remaining_shares
            if is_final_sale:
                cost_basis = position.remaining_stake
            else:
                cost_basis = (
                    position.remaining_stake
                    * shares_to_sell
                    / position.remaining_shares
                )
            remaining_shares = position.remaining_shares - shares_to_sell
            remaining_stake = position.remaining_stake - cost_basis
            proceeds = shares_to_sell * price
            realized_proceeds = position.realized_proceeds + proceeds
            realized_cost_basis = position.realized_cost_basis + cost_basis
            now = _now()
            account = self._account(db, guild_id, user_id)
            db.execute(
                "UPDATE accounts SET balance=?,updated_at=? WHERE guild_id=? AND user_id=?",
                (_amount(account.balance + proceeds), now, guild_id, user_id),
            )
            db.execute(
                "UPDATE positions SET status=?,resolved_at=?,payout=?,outcome_value=?,"
                "remaining_shares=?,remaining_stake=?,realized_proceeds=?,"
                "realized_cost_basis=?,last_activity_at=? "
                "WHERE id=? AND status='open'",
                (
                    "sold" if is_final_sale else "open",
                    now if is_final_sale else None,
                    _amount(realized_proceeds) if is_final_sale else None,
                    _amount(price) if is_final_sale else None,
                    _amount(remaining_shares),
                    _amount(remaining_stake),
                    _amount(realized_proceeds),
                    _amount(realized_cost_basis),
                    now,
                    position_id,
                ),
            )
            db.execute(
                "INSERT INTO ledger(guild_id,user_id,kind,amount,description,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    guild_id,
                    user_id,
                    "sale",
                    _amount(proceeds),
                    f"Sold {shares_to_sell} shares from position {position_id} "
                    f"({position.outcome.upper()} on {position.question})",
                    now,
                ),
            )
            updated_position = Position(
                **{
                    **position.__dict__,
                    "status": "sold" if is_final_sale else "open",
                    "resolved_at": now if is_final_sale else None,
                    "payout": realized_proceeds if is_final_sale else None,
                    "outcome_value": price if is_final_sale else None,
                    "remaining_shares": remaining_shares,
                    "remaining_stake": remaining_stake,
                    "realized_proceeds": realized_proceeds,
                    "realized_cost_basis": realized_cost_basis,
                    "last_activity_at": now,
                }
            )
            return SaleResult(
                position=updated_position,
                shares_sold=shares_to_sell,
                price=price,
                proceeds=proceeds,
                cost_basis=cost_basis,
            )

    def open_positions(self, guild_id: str | None = None) -> list[Position]:
        with self._connection() as db:
            if guild_id is None:
                rows = db.execute(
                    "SELECT * FROM positions WHERE status='open' "
                    "ORDER BY opened_at DESC,id DESC"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM positions WHERE status='open' AND guild_id=? "
                    "ORDER BY opened_at DESC,id DESC",
                    (guild_id,),
                ).fetchall()
            return [self._position(row) for row in rows]

    def leaderboard_accounts(self, guild_id: str) -> list[Account]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT guild_id,user_id,balance FROM accounts WHERE guild_id=?",
                (guild_id,),
            ).fetchall()
            return [
                Account(row["guild_id"], row["user_id"], Decimal(row["balance"])) for row in rows
            ]

    def settle_market(self, market_id: str, outcome_values: dict[str, Decimal]) -> list[Position]:
        """Settle a market once using its final YES/NO payout values."""
        normalized = {key.lower(): value for key, value in outcome_values.items()}
        if not {"yes", "no"}.issubset(normalized):
            return []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM positions WHERE market_id=? AND status='open'", (market_id,)
            ).fetchall()
            now = _now()
            settled: list[Position] = []
            for row in rows:
                position = self._position(row)
                outcome_value = normalized.get(position.outcome)
                if outcome_value is None or not Decimal("0") <= outcome_value <= Decimal("1"):
                    continue
                payout = position.remaining_shares * outcome_value
                account_row = db.execute(
                    "SELECT balance FROM accounts WHERE guild_id=? AND user_id=?",
                    (position.guild_id, position.user_id),
                ).fetchone()
                new_balance = Decimal(account_row["balance"]) + payout
                db.execute(
                    "UPDATE accounts SET balance=?,updated_at=? WHERE guild_id=? AND user_id=?",
                    (_amount(new_balance), now, position.guild_id, position.user_id),
                )
                db.execute(
                    "UPDATE positions SET status='settled',resolved_at=?,payout=?,outcome_value=?,"
                    "remaining_shares='0',remaining_stake='0',last_activity_at=? "
                    "WHERE id=? AND status='open'",
                    (now, _amount(payout), _amount(outcome_value), now, position.id),
                )
                db.execute(
                    "INSERT INTO ledger(guild_id,user_id,kind,amount,description,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        position.guild_id,
                        position.user_id,
                        "settlement",
                        _amount(payout),
                        f"{position.outcome.upper()} resolved for {market_id}",
                        now,
                    ),
                )
                settled.append(
                    Position(
                        **{
                            **position.__dict__,
                            "status": "settled",
                            "resolved_at": now,
                            "payout": payout,
                            "outcome_value": outcome_value,
                            "remaining_shares": Decimal("0"),
                            "remaining_stake": Decimal("0"),
                            "last_activity_at": now,
                        }
                    )
                )
            return settled

    def reset(self, guild_id: str, user_id: str | None = None) -> tuple[int, int]:
        """Reset one or all existing accounts in a guild; return (accounts, voided bets)."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            if user_id is None:
                accounts = db.execute(
                    "SELECT user_id,balance FROM accounts WHERE guild_id=?", (guild_id,)
                ).fetchall()
                db.execute(
                    "UPDATE positions SET status='void',resolved_at=?,outcome_value=NULL,"
                    "payout=NULL,remaining_shares='0',remaining_stake='0',last_activity_at=? "
                    "WHERE guild_id=? AND status='open'",
                    (now, now, guild_id),
                )
                voided = db.execute(
                    "SELECT changes() AS count"
                ).fetchone()["count"]
                db.execute(
                    "UPDATE accounts SET balance=?,updated_at=? WHERE guild_id=?",
                    (_amount(STARTING_BALANCE), _now(), guild_id),
                )
            else:
                self._ensure_account(db, guild_id, user_id)
                accounts = db.execute(
                    "SELECT user_id,balance FROM accounts WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchall()
                db.execute(
                    "UPDATE positions SET status='void',resolved_at=?,outcome_value=NULL,"
                    "payout=NULL,remaining_shares='0',remaining_stake='0',last_activity_at=? "
                    "WHERE guild_id=? AND user_id=? AND status='open'",
                    (now, now, guild_id, user_id),
                )
                voided = db.execute("SELECT changes() AS count").fetchone()["count"]
                db.execute(
                    "UPDATE accounts SET balance=?,updated_at=? WHERE guild_id=? AND user_id=?",
                    (_amount(STARTING_BALANCE), _now(), guild_id, user_id),
                )

            for row in accounts:
                delta = STARTING_BALANCE - Decimal(row["balance"])
                db.execute(
                    "INSERT INTO ledger(guild_id,user_id,kind,amount,description,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        guild_id,
                        row["user_id"],
                        "admin_reset",
                        _amount(delta),
                        "Balance reset to 10,000 credits; open positions voided",
                        now,
                    ),
                )
            return len(accounts), voided
