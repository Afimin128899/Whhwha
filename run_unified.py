"""Entry point for the new single, administrator-only Telegram bot."""
import asyncio

from unified_bot.app import main

if __name__ == "__main__":
    asyncio.run(main())
