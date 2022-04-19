import asyncio

from hypercorn import Config
from hypercorn.asyncio import serve as serve_hypercorn

from localstack.aws.gateway import Gateway

from .asgi import AsgiGateway


def serve(gateway: Gateway, host="localhost", port=4566, use_reloader=True, **kwargs):
    config = Config()
    config.bind = f"{host}:{port}"
    config.use_reloader = use_reloader

    for k, v in kwargs.items():
        setattr(config, k, v)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(serve_hypercorn(AsgiGateway(gateway), config))
