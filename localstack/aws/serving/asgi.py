from concurrent.futures import ThreadPoolExecutor

from localstack.aws.gateway import Gateway
from localstack.aws.serving.wsgi import WsgiGateway
from localstack.http.asgi import ASGIAdapter


class AsgiGateway:

    gateway: Gateway

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.asgi = ASGIAdapter(WsgiGateway(gateway), executor=ThreadPoolExecutor(100))

    async def __call__(self, scope, receive, send):
        print("invoking scope", scope)
        if scope["type"] == "http":
            return await self.asgi(scope, receive, send)

        raise NotImplementedError(f"{scope['type']} protocol is not implemented")
