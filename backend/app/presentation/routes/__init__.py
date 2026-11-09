"""Pacote de rotas da camada de apresentação da aplicação FastAPI."""

from .health import router as health_router

__all__ = ["health_router"]
