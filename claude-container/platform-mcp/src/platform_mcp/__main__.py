import logging

from mcp.server.fastmcp import FastMCP

from .tools import register_tools


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp = FastMCP("platform-mcp")
    register_tools(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
