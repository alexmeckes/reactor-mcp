"""Inspect the real stdio server with live operations explicitly disabled."""

import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reactor_mcp.server"],
        env={"PATH": os.environ.get("PATH", ""), "REACTOR_ENABLE_LIVE": "false"},
    )
    async with asyncio.timeout(15):
        async with Client(params) as client:
            print(f"Connected: {client.server_info.name} ({client.protocol_version})")
            for tool in (await client.list_tools()).tools:
                print(f"  {tool.name}")
            info = await client.call_tool("reactor_describe", {})
            print(json.dumps(info.structured_content, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
