"""Desktop-only MCP view of official CuaDriver. No computer actions live here.

The exact allowlist omits browser attachment/navigation, arbitrary script/batch
execution and permission/setup tools. Native browser tools and Playwright remain
separate. Tool payloads, including images and error content, pass unchanged.
"""
from __future__ import annotations
import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import signal
import sys
import threading

# Official CuaDriver 0.37.0 catalog. Unknown future tools are not auto-exposed.
DESKTOP_TOOLS = frozenset({
    "get_accessibility_tree", "get_desktop_state", "get_window_state",
    "list_apps", "list_windows", "get_cursor_position", "get_screen_size",
    "click", "double_click", "right_click", "drag", "scroll", "type_text",
    "press_key", "hotkey", "set_value", "move_cursor", "bring_to_front",
    "launch_app", "invoke_menu", "set_window_frame", "verify_state", "zoom",
    "get_guide", "get_session", "end_session",
})


def server_spec(executable):
    from . import settings
    return {"name": "kaus-cua-driver", "command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--driver", os.fspath(executable)], "env": [{"name": "KAUS_COMPUTER_USE_HOME",
                "value": str(settings.storage_root())}]}


def _arm_retirement():
    # The SDK's stdio reader can remain in a blocking thread until its parent
    # closes stdin. Only this bridge process is disposable; its durable action
    # receipt survives. Give owned MCP cleanup a bounded chance, then let the
    # resource reconciler establish daemon completion independently.
    timer = threading.Timer(5, os._exit, args=(70,))
    timer.daemon = True
    timer.start()


async def serve(executable, *, transport_factory=None, operation_factory=None, on_uncertain=None):
    from mcp import types
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from drivers.session_mcp.stdio_compat import stdio_client
    from drivers.computer_use import installer
    from drivers.computer_use.cua_guard import Operation, _peer_identity
    socket = await installer.ensure_runtime_host(executable=executable, authorized_runtime=True)
    transport_factory = transport_factory or stdio_client
    managed_operations = operation_factory is None
    uncertain = asyncio.Event()
    with open(os.devnull, "w") as errors:
        def connection():
            transport = transport_factory(StdioServerParameters(command=executable,
                args=["mcp", "--socket", os.fspath(socket)]), errlog=errors)
            return Client(transport, read_timeout_seconds=60)
        async with stdio_server() as (reader, writer):
            async with AsyncExitStack() as upstream:
                client = await upstream.enter_async_context(connection())
                generation = await asyncio.to_thread(_peer_identity, socket) if managed_operations else None
                reconnects = asyncio.Queue()
                async def prepare_call():
                    # The MCP catalog can be initialized before a Group obtains
                    # desktop ownership. A prior owner's recovery can then retire
                    # that host. Reconnect before dispatch, never replay an RPC.
                    await installer.ensure_runtime_host(executable=executable, authorized_runtime=True)
                    current = await asyncio.to_thread(_peer_identity, socket)
                    if current != generation:
                        ready = asyncio.get_running_loop().create_future()
                        await reconnects.put(ready)
                        await ready
                if managed_operations:
                    operation_factory = lambda command, endpoint: Operation(command, endpoint, prepare=prepare_call)
                catalog, cursor, seen = [], None, set()
                for _ in range(16):
                    page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                    catalog.extend(tool for tool in page.tools if tool.name in DESKTOP_TOOLS)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("Invalid MCP catalog pagination")
                    seen.add(cursor)
                else:
                    raise ValueError("MCP catalog exceeds limit")
                names = {tool.name for tool in catalog}
                async def list_tools(context, params):
                    return types.ListToolsResult(tools=catalog)
                async def call_tool(context, params):
                    if params.name not in names:
                        return types.CallToolResult(isError=True,
                            content=[types.TextContent(type="text", text="Tool unavailable")])
                    operation = operation_factory(executable, socket)
                    try:
                        async with operation:
                            return await client.call_tool(params.name, params.arguments or {}, read_timeout_seconds=60)
                    except BaseException:
                        # A timeout/cancel may leave a daemon RPC running. Retire
                        # this proxy so durable recovery can stop its exact orphan
                        # host; never acknowledge that action by just closing stdio.
                        # Cancelling a queued call that never dispatched must not
                        # retire another active call owned by this same proxy.
                        if operation.dispatched and not uncertain.is_set():
                            uncertain.set()
                            if on_uncertain is not None:
                                on_uncertain()
                        raise
                server = Server("kaus-cua-desktop", version="1", on_list_tools=list_tools, on_call_tool=call_tool)
                serving = asyncio.create_task(server.run(reader, writer, server.create_initialization_options()))
                failed = asyncio.create_task(uncertain.wait())
                reconnect = asyncio.create_task(reconnects.get())
                try:
                    while True:
                        await asyncio.wait((serving, failed, reconnect), return_when=asyncio.FIRST_COMPLETED)
                        if serving.done():
                            await serving
                            break
                        if failed.done():
                            break
                        ready = reconnect.result()
                        try:
                            # MCP transport contexts must close in the task that
                            # entered them. This owner handles reconnection for
                            # the tool callback waiting under the operation lock.
                            await upstream.aclose()
                            client = await upstream.enter_async_context(connection())
                            generation = await asyncio.to_thread(_peer_identity, socket)
                            if not ready.done():
                                ready.set_result(None)
                        except Exception as error:
                            if not ready.done():
                                ready.set_exception(error)
                        reconnect = asyncio.create_task(reconnects.get())
                finally:
                    for task in (serving, failed, reconnect):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(serving, failed, reconnect, return_exceptions=True)


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver", required=True)
    args = parser.parse_args()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await serve(args.driver, on_uncertain=_arm_retirement)
    except asyncio.CancelledError:
        return 130
    return 0


if __name__ == "__main__":
    # The native Agent starts this entrypoint by absolute path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(asyncio.run(main()))
