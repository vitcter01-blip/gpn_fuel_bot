"""Регрессии главного меню: python test_bot_menu.py"""
import asyncio
import os
import tempfile
from pathlib import Path


GOOD_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


class FakeMessage:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


async def check_start_menu() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOT_TOKEN"] = GOOD_TOKEN
        os.environ["DB_PATH"] = str(Path(tmp) / "menu.db")

        import bot

        message = FakeMessage()
        await bot.on_start(message)

        assert len(message.answers) == 1, "Команда /start не должна дублировать меню"
        keyboard = message.answers[0][1]["reply_markup"]
        assert keyboard == bot.main_kb(), "Главное меню должно быть нижней клавиатурой"
        assert keyboard.is_persistent is True, "Нижнее меню должно оставаться закреплённым"


if __name__ == "__main__":
    asyncio.run(check_start_menu())
    print("PASS: главное меню одно и закреплено")
