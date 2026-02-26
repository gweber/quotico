"""
backend/verify.py

Purpose:
    CLI entrypoint for the permanent core Tower health check.

Dependencies:
    - app.database
    - app.checks.tower_check
"""

import asyncio
import os
import sys
from pprint import pprint

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.checks.tower_check import TowerHealthCheck
from app.database import close_db, connect_db


async def main() -> int:
    print("\nSTARTING QUOTICO CORE HEALTH CHECK")
    print("=" * 50)

    try:
        await connect_db()
        report = await TowerHealthCheck.run()

        print("\n--- REPORT ---")
        pprint(report, indent=2)
        print("-" * 50)

        if report.get("status") == "HEALTHY":
            print("\nSYSTEM GREEN: The Tower is standing.")
            print("Database, Leagues, and Team Registry are operational.")
            return 0

        print(f"\nSYSTEM RED: Status is {report.get('status')}")
        print("Check the error report above.")
        return 1
    except ImportError as e:
        print(f"SETUP ERROR: Could not import app modules.\n{e}")
        return 1
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        return 1
    finally:
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

