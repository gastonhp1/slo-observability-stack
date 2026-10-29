"""ASGI entrypoint: ``uvicorn shop.main:app``."""

from . import config
from .app import create_app

app = create_app(config.load())
