import json
import logging
from queue import Queue, Empty
from threading import Event, Thread
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from httpx_sse import connect_sse


def remove_request_params(url: str) -> str:
    return urljoin(url, urlparse(url).path)


class McpClient:
    def __init__(self, name: str, url: str,
                 headers: dict[str, Any] | None = None,
                 timeout: float = 60,
                 sse_read_timeout: float = 60 * 5,
                 ):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self.endpoint_url = None
        self.client = httpx.Client(headers=headers)
        self._request_id = 0
        self.message_queue = Queue()
        self.response_ready = Event()
        self.should_stop = Event()
        self._listen_thread = None
        self._connected = Event()
        self._error_event = Event()
        self._thread_exception = None
        self.connect()

    def _listen_messages(self) -> None:
        try:
            logging.info(f"{self.name} - Connecting to SSE endpoint: {remove_request_params(self.url)}")
            with connect_sse(
                    client=self.client,
                    method="GET",
                    url=self.url,
                    timeout=httpx.Timeout(self.timeout, read=self.sse_read_timeout),
            ) as event_source:
                event_source.response.raise_for_status()
                logging.debug(f"{self.name} - SSE connection established")
                for sse in event_source.iter_sse():
                    logging.debug(f"{self.name} - Received SSE event: {sse.event}")
                    if self.should_stop.is_set():
                        break
                    match sse.event:
                        case "endpoint":
                            self.endpoint_url = urljoin(self.url, sse.data)
                            logging.info(f"{self.name} - Received endpoint URL: {self.endpoint_url}")
                            self._connected.set()
                            url_parsed = urlparse(self.url)
                            endpoint_parsed = urlparse(self.endpoint_url)
                            if (url_parsed.netloc != endpoint_parsed.netloc
                                    or url_parsed.scheme != endpoint_parsed.scheme):
                                error_msg = f"{self.name} - Endpoint origin does not match connection origin: {self.endpoint_url}"
                                logging.error(error_msg)
                                raise ValueError(error_msg)
                        case "message":
                            message = json.loads(sse.data)
                            logging.debug(f"{self.name} - Received server message: {message}")
                            self.message_queue.put(message)
                            self.response_ready.set()
                        case _:
                            logging.warning(f"{self.name} - Unknown SSE event: {sse.event}")
        except Exception as e:
            self._thread_exception = e
            self._error_event.set()
            self._connected.set()

    def send_message(self, data: dict):
        if not self.endpoint_url:
            if self._thread_exception:
                raise ConnectionError(f"{self.name} - MCP Server connection failed: {self._thread_exception}")
            else:
                raise RuntimeError(f"{self.name} - Please call connect() first")
        logging.debug(f"{self.name} - Sending client message: {data}")
        response = self.client.post(
            url=self.endpoint_url,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=self.timeout
        )
        response.raise_for_status()
        logging.debug(f"{self.name} - Client message sent successfully: {response.status_code}")
        if "id" in data:
            message_id = data["id"]
            while True:
                self.response_ready.wait()
                self.response_ready.clear()
                try:
                    while True:
                        message = self.message_queue.get_nowait()
                        if "id" in message and message["id"] == message_id:
                            self._request_id += 1
                            return message
                        self.message_queue.put(message)
                except Empty:
                    pass
        return {}

    def connect(self) -> None:
        self._listen_thread = Thread(target=self._listen_messages, daemon=True)
        self._listen_thread.start()
        while True:
            if self._error_event.is_set():
                if isinstance(self._thread_exception, httpx.HTTPStatusError):
                    raise ConnectionError(f"{self.name} - MCP Server connection failed: {self._thread_exception}") \
                        from self._thread_exception
                else:
                    raise self._thread_exception
            if self._connected.wait(timeout=0.1):
                break
            if not self._listen_thread.is_alive():
                raise ConnectionError(f"{self.name} - MCP Server SSE listener thread died unexpectedly!")

    def close(self) -> None:
        try:
            self.should_stop.set()
            self.client.close()
            if self._listen_thread and self._listen_thread.is_alive():
                self._listen_thread.join(timeout=10)
        except Exception as e:
            raise Exception(f"{self.name} - MCP Server connection close failed: {str(e)}")

    def initialize(self):
        init_data = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp",
                    "version": "0.1.0"
                }
            }
        }
        self.send_message(init_data)
        notify_data = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        self.send_message(notify_data)

    def list_tools(self):
        tools_data = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/list",
            "params": {}
        }
        return self.send_message(tools_data).get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, tool_args: dict):
        call_data = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args
            }
        }
        return self.send_message(call_data).get("result", {}).get("content", [])


class McpClients:
    def __init__(self, servers_config: dict[str, Any]):
        if "mcpServers" in servers_config:
            servers_config = servers_config["mcpServers"]
        self._clients = {
            name: McpClient(
                name=name,
                url=config.get("url"),
                headers=config.get("headers", None),
                timeout=config.get("timeout", 60),
                sse_read_timeout=config.get("sse_read_timeout", 300),
            )
            for name, config in servers_config.items()
        }
        for client in self._clients.values():
            client.initialize()
        self._tools = {}

    def fetch_tools(self) -> list[dict]:
        try:
            all_tools = []
            for server_name, client in self._clients.items():
                tools = client.list_tools()
                all_tools.extend(tools)
                self._tools[server_name] = tools
            return all_tools
        except Exception as e:
            raise RuntimeError(f"Error fetching tools: {str(e)}")

    def execute_tool(self, tool_name: str, tool_args: dict[str, Any]):
        if not self._tools:
            self.fetch_tools()
        tool_clients = {}
        for server_name, tools in self._tools.items():
            for tool in tools:
                if server_name in self._clients:
                    tool_clients[tool["name"]] = self._clients[server_name]
        client = tool_clients.get(tool_name)
        try:
            result = client.call_tool(tool_name, tool_args)
            if isinstance(result, dict) and "progress" in result:
                progress = result["progress"]
                total = result["total"]
                percentage = (progress / total) * 100
                logging.info(
                    f"Progress: {progress}/{total} "
                    f"({percentage:.1f}%)"
                )
            return f"Tool execution result: {result}"
        except Exception as e:
            error_msg = f"Error executing tool: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception as e:
                logging.error(e)
