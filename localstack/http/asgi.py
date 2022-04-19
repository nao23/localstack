import asyncio
import io
import logging
import math
import time
import typing as t
from asyncio import AbstractEventLoop
from concurrent.futures import Executor
from urllib.parse import urlparse

if t.TYPE_CHECKING:
    from _typeshed import WSGIApplication, WSGIEnvironment
    from asgiref.typing import (
        ASGI3Application,
        ASGIReceiveCallable,
        ASGISendCallable,
        HTTPScope,
        Scope,
    )

LOG = logging.getLogger(__name__)


def _populate_standard_keys(environ: "WSGIEnvironment", scope: "HTTPScope"):
    environ["REQUEST_METHOD"] = scope["method"]
    # path/uri info
    environ["SCRIPT_NAME"] = scope.get("root_path", "").rstrip("/")
    path = scope["path"]
    path = path if path[0] == "/" else urlparse(path).path
    environ["PATH_INFO"] = path
    environ["RAW_URI"] = scope["raw_path"]

    query_string = scope.get("query_string")
    if query_string:
        environ["QUERY_STRING"] = query_string.decode("latin1")

    # server address / host
    server = scope.get("server") or ("localhost", 80)
    environ["SERVER_NAME"] = server[0]
    environ["SERVER_PORT"] = str(server[1]) if server[1] else "80"

    # http version
    environ["SERVER_PROTOCOL"] = "HTTP/" + scope["http_version"]

    # client (remote) address
    client = scope.get("client")
    if client:
        environ["REMOTE_ADDR"] = client[0]
        environ["REMOTE_PORT"] = str(client[1])

    # headers
    for name, value in scope["headers"]:
        key = name.decode("latin1").upper().replace("-", "_")
        environ[f"HTTP_{key}"] = value.decode("latin1")

    if "HTTP_CONTENT_TYPE" in environ:
        environ["CONTENT_TYPE"] = environ["HTTP_CONTENT_TYPE"]

    if "HTTP_CONTENT_LENGTH" in environ:
        environ["CONTENT_LENGTH"] = environ["HTTP_CONTENT_LENGTH"]


def _populate_wsgi_keys(environ: "WSGIEnvironment", scope: "HTTPScope"):
    environ["wsgi.version"] = (1, 0)
    environ["wsgi.url_scheme"] = scope.get("scheme", "http")
    environ["wsgi.errors"] = io.BytesIO()
    environ["wsgi.multithread"] = True
    environ["wsgi.multiprocess"] = False
    environ["wsgi.run_once"] = False


class HTTPRequestEventStreamAdapter:
    """
    Minimal adapter to expose an ASGIReceiveCallable coroutine that returns HTTPRequestEvent
    instances, as an IO[bytes] stream for consumption in synchronous WSGI/Werkzeug code.
    """

    def __init__(
        self, receive: "ASGIReceiveCallable", event_loop: t.Optional[AbstractEventLoop] = None
    ) -> None:
        super().__init__()
        self.receive = receive
        self.event_loop = event_loop or asyncio.get_event_loop()

    def read(self, size: int = -1) -> bytes:
        # TODO: better implementation

        if size != -1:
            raise NotImplementedError("can only read from stream exhaustively")

        arr = bytearray()

        more = True
        while more:
            recv_future = asyncio.run_coroutine_threadsafe(self.receive(), self.event_loop)
            event = recv_future.result()
            body = event["body"]
            if body:
                arr.extend(body)
            more = event.get("more_body", False)
        print(arr)

        return arr


class WsgiStartResponse:
    def __init__(
        self,
        send: "ASGISendCallable",
        event_loop: AbstractEventLoop = None,
    ):
        self.send = send
        self.event_loop = event_loop or asyncio.get_event_loop()
        self.sent = 0
        self.content_length = math.inf
        self.finalized = False
        self.started = False

    def __call__(
        self, status: str, headers: t.List[t.Tuple[str, str]], exec_info=None
    ) -> t.Callable[[bytes], t.Any]:
        return self.start_response_sync(status, headers, exec_info)

    def start_response_sync(
        self, status: str, headers: t.List[t.Tuple[str, str]], exec_info=None
    ) -> t.Callable[[bytes], t.Any]:
        send = self.send
        loop = self.event_loop

        # start sending response
        asyncio.run_coroutine_threadsafe(
            send(
                {
                    "type": "http.response.start",
                    "status": int(status[:3]),
                    "headers": [(h[0].encode("latin1"), h[1].encode("latin1")) for h in headers],
                }
            ),
            loop,
        ).result()

        self.started = True
        # find out content length if set
        self.content_length = math.inf  # unknown content-length
        for k, v in headers:
            if k.lower() == "content-length":
                self.content_length = int(v)
                break

        return self.write_sync

    def write_sync(self, data: bytes) -> None:
        return asyncio.run_coroutine_threadsafe(self.write(data), self.event_loop).result()

    async def write(self, data: bytes) -> None:
        if not self.started:
            raise ValueError("not started the response yet")
        self.sent += len(data)
        more_body = self.sent < self.content_length
        if not more_body:
            self.finalized = True
        await self.send({"type": "http.response.body", "body": data, "more_body": more_body})

    async def close(self):
        if not self.started:
            raise ValueError("not started the response yet")

        if not self.finalized:
            self.finalized = True
            await self.send({"type": "http.response.body", "body": b"", "more_body": False})


class ASGIAdapter:
    """
    Adapter to expose a WSGIApplication as an ASGI3Application. This allows you to serve synchronous WSGI applications
    through ASGI servers (e.g., Hypercorn).

    https://asgi.readthedocs.io/en/latest/specs/main.html
    """

    def __init__(
        self,
        wsgi_app: "WSGIApplication",
        event_loop: AbstractEventLoop = None,
        executor: Executor = None,
    ):
        self.wsgi_app = wsgi_app
        self.event_loop = event_loop or asyncio.get_event_loop()
        self.executor = executor

    async def __call__(
        self, scope: "Scope", receive: "ASGIReceiveCallable", send: "ASGISendCallable"
    ):
        """
        The ASGI 3 interface. Can only handle HTTP calls.

        :param scope: the connection scope
        :param receive: the receive callable
        :param send: the send callable
        """
        if scope["type"] == "http":
            return await self.handle_http(scope, receive, send)

        raise NotImplementedError("Unhandled protocol %s" % scope["type"])

    def to_wsgi_environment(
        self,
        scope: "HTTPScope",
        receive: "ASGIReceiveCallable",
    ) -> "WSGIEnvironment":
        """
        Creates an IO-ready WSGIEnvironment from the given ASGI HTTP call.

        :param scope: the ASGI HTTP Scope
        :param receive: the ASGI callable to receive the HTTP request
        :return: a WSGIEnvironment
        """
        environ: "WSGIEnvironment" = {}
        _populate_standard_keys(environ, scope)
        _populate_wsgi_keys(environ, scope)
        # add IO wrappers
        environ["wsgi.input"] = HTTPRequestEventStreamAdapter(receive, event_loop=self.event_loop)
        environ["wsgi.input_terminated"] = True
        return environ

    async def handle_http(
        self, scope: "HTTPScope", receive: "ASGIReceiveCallable", send: "ASGISendCallable"
    ):
        env = self.to_wsgi_environment(scope, receive)
        response = WsgiStartResponse(send, self.event_loop)

        data = await self.event_loop.run_in_executor(self.executor, self.wsgi_app, env, response)

        if data:
            for packet in data:
                await response.write(packet)

        await response.close()


def main():
    def create_app() -> "WSGIApplication":
        from werkzeug import Request, Response

        def gen():
            yield b"foo"
            time.sleep(1)
            yield b"bar\n"
            time.sleep(1)
            yield b"ed\n"

        @Request.application
        def app(request: Request) -> Response:
            print("making request")
            print("data: ", request.get_data(as_text=True))
            time.sleep(1)
            print("making response")

            response = Response(gen(), 200)

            return response

        return app

    wsgi_app = create_app()
    asgi_app = ASGI3Application(wsgi_app)

    from hypercorn import Config
    from hypercorn.asyncio import serve
    from hypercorn.typing import ASGI3Framework

    cfg = Config()
    cfg.use_reloader = True

    loop = asyncio.get_event_loop()
    hypercorn_app = t.cast(ASGI3Framework, asgi_app)
    loop.run_until_complete(serve(hypercorn_app, cfg))


if __name__ == "__main__":
    main()
