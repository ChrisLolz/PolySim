import asyncio
from pathlib import Path

from polysim.bot import create_bot
from polysim.config import Settings


def test_bot_registers_expected_public_slash_commands():
    bot = create_bot(Settings("test-token", Path("/tmp/polysim-test.sqlite3")))
    try:
        commands = {command.name: command for command in bot.tree.get_commands()}
        assert {"markets", "bet", "balance", "positions", "leaderboard", "admin"} == set(
            commands
        )
        assert {command.name for command in commands["admin"].commands} == {"reset"}
    finally:
        asyncio.run(bot.close())
