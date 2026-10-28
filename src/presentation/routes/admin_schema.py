"""Rota ``GET /admin/schema`` (FA-03): o que a UI pode oferecer ao montar a config.

Só a chave admin chega aqui (prefixo ``/admin`` em ``api_key_auth``). O schema é montado no composition
root a partir do código em vigor (registry de providers com os plugins da allowlist, entidades e store) e
o lifespan o põe em ``app.state.config_schema``: provider novo aparece sem mudar a rota.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.application.use_cases.config_schema import ConfigSchema

router = APIRouter(prefix="/admin", tags=["admin-config"])


@router.get("/schema", name="config_schema")
async def config_schema(request: Request) -> JSONResponse:
    """Providers por tipo, enumerações das entidades, formatos de ``api_key_ref`` e se o store é gravável."""
    schema = getattr(request.app.state, "config_schema", None)
    if not isinstance(schema, ConfigSchema):
        raise HTTPException(503, "Schema da configuração indisponível")
    return JSONResponse(schema.document())
