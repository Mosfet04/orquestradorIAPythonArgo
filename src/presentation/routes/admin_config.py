"""Rotas ``/admin/config/{agents,teams,tools}`` (FA-02): CRUD da config com a versão no ETag.

Só a chave admin chega aqui (prefixo ``/admin`` em ``api_key_auth``). Corpo, query e ``If-Match`` são
lidos à mão, sem modelo do FastAPI: a validação é a das entidades (no caso de uso) e o 422 padrão do
FastAPI devolveria o valor recebido (``input``). Nenhuma resposta usa o texto de uma exceção. Corpo
acima de ``MAX_BODY_BYTES`` é 413, contado na leitura do stream (vale também sem ``Content-Length``).

``PUT`` e ``DELETE`` exigem ``If-Match`` com a versão lida (ausente: 428; outra versão: 412).
``DELETE`` é lógico (``active: false``); ``?hard=true`` remove o documento. O ``version`` que a resposta
acrescenta ao documento pode voltar no corpo (a UI reenvia o que leu) e é descartado: vale o ``If-Match``.
"""

from __future__ import annotations

import json
from typing import get_args

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src.application.use_cases.config_admin_use_case import (
    ConfigAdmin,
    ConfigAlreadyExists,
    ConfigDocument,
    ConfigEditorOfAnyKind,
    ConfigInUse,
    ConfigKind,
    ConfigNotFound,
    InvalidConfig,
)
from src.domain.repositories.versioning import ConfigStoreReadOnly, ConfigVersionConflict, Versioned

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_BODY_BYTES = 1024 * 1024
# Versão no If-Match: inteiro curto (int() de um texto gigante custa CPU e tem teto no Python).
_MAX_VERSION_DIGITS = 18
# Chave da versão no documento que a API mostra (não é campo da entidade).
_VERSION = "version"

# Recusa do caso de uso ou da porta -> (status, texto fixo); nunca o texto da exceção.
_REFUSALS: dict[type[Exception], tuple[int, str]] = {
    InvalidConfig: (422, "Configuração inválida"),
    ConfigNotFound: (404, "Config não encontrada"),
    ConfigAlreadyExists: (409, "Já existe config com este id"),
    ConfigInUse: (409, "Config em uso"),
    ConfigStoreReadOnly: (409, "O store de configuração é somente leitura"),
    ConfigVersionConflict: (412, "Versão divergente: releia a config"),
}
_USE_CASE_ERRORS = tuple(_REFUSALS)

router = APIRouter(prefix="/admin/config", tags=["admin-config"])


def _editor(request: Request, kind: ConfigKind) -> ConfigEditorOfAnyKind:
    """Editor do tipo, do caso de uso que o lifespan pôs em ``app.state.config_admin``."""
    admin = getattr(request.app.state, "config_admin", None)
    if not isinstance(admin, ConfigAdmin):
        raise HTTPException(503, "Administração da configuração indisponível")
    return admin.editor(kind)


async def _limited_body(request: Request) -> bytes:
    """Corpo lido do stream até ``MAX_BODY_BYTES``; acima disso, 413 sem ler o resto."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, f"Corpo maior que {MAX_BODY_BYTES} bytes")
    return bytes(body)


async def _json_object(request: Request) -> dict[str, object]:
    """Corpo como objeto JSON; senão ``InvalidConfig`` (422). Operador e aninhamento: no caso de uso."""
    try:
        body = json.loads(await _limited_body(request))
    except (ValueError, RecursionError):
        raise InvalidConfig() from None
    if not isinstance(body, dict):
        raise InvalidConfig()
    body.pop(_VERSION, None)
    return body


def _if_match(request: Request) -> int:
    """Versão do ``If-Match`` (``"3"`` ou ``3``). Ausente: 428; não é versão: 412 (não casa com nenhuma)."""
    raw = request.headers.get("if-match")
    if raw is None:
        raise HTTPException(428, "If-Match obrigatório com o ETag (versão) lido")
    tag = raw.strip().removeprefix('"').removesuffix('"')
    if not (tag.isascii() and tag.isdigit() and len(tag) <= _MAX_VERSION_DIGITS):
        raise HTTPException(412, "Versão divergente: releia a config")
    return int(tag)


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit() and len(raw) <= len(str(maximum)) and minimum <= int(raw) <= maximum):
        raise HTTPException(422, f"{name} deve ser inteiro entre {minimum} e {maximum}")
    return int(raw)


def _hard(request: Request) -> bool:
    raw = request.query_params.get("hard", "false")
    if raw not in ("true", "false"):
        raise HTTPException(422, "hard deve ser true ou false")
    return raw == "true"


def _body(item: Versioned[ConfigDocument]) -> ConfigDocument:
    return {**item.entity, _VERSION: item.version}


def _item(item: Versioned[ConfigDocument], status_code: int = 200) -> JSONResponse:
    return JSONResponse(_body(item), status_code=status_code, headers={"ETag": f'"{item.version}"'})


def _error(exc: Exception) -> JSONResponse:
    """Resposta de uma recusa do caso de uso ou da porta (``_REFUSALS``), com onde e quem, sem valor."""
    status_code, detail = next(refusal for kind, refusal in _REFUSALS.items() if isinstance(exc, kind))
    body: dict[str, object] = {"detail": detail}
    if isinstance(exc, InvalidConfig):
        if exc.field:
            body["field"] = exc.field
        if exc.indexes:
            body["indexes"] = list(exc.indexes)
    if isinstance(exc, ConfigInUse):
        body["used_by"] = exc.used_by
    return JSONResponse(body, status_code=status_code)


def _add_routes(kind: ConfigKind) -> None:
    """``GET``/``POST`` na coleção e ``GET``/``PUT``/``DELETE`` no item, para um tipo de config."""

    @router.get(f"/{kind}", name=f"list_{kind}")
    async def list_configs(request: Request) -> Response:
        offset = _query_int(request, "offset", 0, 0, 10**9)
        limit = _query_int(request, "limit", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
        items = await _editor(request, kind).list_all()
        page = [_body(item) for item in items[offset : offset + limit]]
        return JSONResponse({"items": page, "total": len(items), "offset": offset, "limit": limit})

    @router.get(f"/{kind}/{{entity_id}}", name=f"get_{kind}")
    async def get_config(request: Request, entity_id: str) -> Response:
        try:
            return _item(await _editor(request, kind).get(entity_id))
        except _USE_CASE_ERRORS as exc:
            return _error(exc)

    @router.post(f"/{kind}", name=f"create_{kind}", status_code=201)
    async def create_config(request: Request) -> Response:
        try:
            return _item(await _editor(request, kind).create(await _json_object(request)), status_code=201)
        except _USE_CASE_ERRORS as exc:
            return _error(exc)

    @router.put(f"/{kind}/{{entity_id}}", name=f"replace_{kind}")
    async def replace_config(request: Request, entity_id: str) -> Response:
        expected_version = _if_match(request)
        try:
            body = await _json_object(request)
            return _item(await _editor(request, kind).replace(entity_id, body, expected_version))
        except _USE_CASE_ERRORS as exc:
            return _error(exc)

    @router.delete(f"/{kind}/{{entity_id}}", name=f"delete_{kind}")
    async def delete_config(request: Request, entity_id: str) -> Response:
        expected_version = _if_match(request)
        hard = _hard(request)
        editor = _editor(request, kind)
        try:
            if hard:
                await editor.remove(entity_id, expected_version)
                return Response(status_code=204)
            return _item(await editor.deactivate(entity_id, expected_version))
        except _USE_CASE_ERRORS as exc:
            return _error(exc)


for _kind in get_args(ConfigKind):
    _add_routes(_kind)
