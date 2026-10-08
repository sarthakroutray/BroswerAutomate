"""`python -m server` entry point — same as the `browser-automation-mcp` console script."""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)


def main() -> None:
    from .transport import run_mcp_server

    asyncio.run(run_mcp_server())


if __name__ == "__main__":
    main()
