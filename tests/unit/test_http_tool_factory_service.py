import json
import httpx
from unittest.mock import patch, Mock

from src.application.services.http_tool_factory_service import HttpToolFactory
from src.domain.entities.tool import Tool, ToolParameter, ParameterType, HttpMethod


def make_tool(method: HttpMethod, route: str = "http://api/x/{id}") -> Tool:
    return Tool(
        id="t1",
        name="T1",
        description="Desc",
        route=route,
        http_method=method,
        parameters=[ToolParameter(name="p", type=ParameterType.STRING, description="d")],
        instructions="",
        headers={}
    )


def test_create_tools_from_configs_success_get_and_post(monkeypatch):
    # Dummy Toolkit para capturar funções registradas
    class DummyToolkit:
        def __init__(self, name, instructions):
            self.name = name
            self.instructions = instructions
            self._functions = {}

        def register(self, function, name):
            self._functions.setdefault(name, []).append(function)

    # Monkeypatch Toolkit dentro do módulo alvo
    import src.application.services.http_tool_factory_service as mod
    monkeypatch.setattr(mod, "Toolkit", DummyToolkit)

    factory = HttpToolFactory()
    tools = [make_tool(HttpMethod.GET), make_tool(HttpMethod.POST)]

    with patch("httpx.request") as req:
        mock_resp = Mock(status_code=200)
        mock_resp.content = b"{}"
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"ok": True}
        mock_resp.text = json.dumps({"ok": True})
        req.return_value = mock_resp
        agno_tools = factory.create_tools_from_configs(tools)

        assert len(agno_tools) == 2

        # Executa função GET com placeholder
        fn_get = agno_tools[0]._functions["t1"][0]
        result_get = fn_get(id={"id": 1})
        assert "ok" in result_get

        # Executa função POST
        fn_post = agno_tools[1]._functions["t1"][0]
        result_post = fn_post(id={"id": 2}, body={"x": 1})
        assert "ok" in result_post


def test_http_errors_are_captured_and_returned(monkeypatch):
    class DummyToolkit:
        def __init__(self, name, instructions):
            self.name = name
            self.instructions = instructions
            self._functions = {}

        def register(self, function, name):
            self._functions.setdefault(name, []).append(function)

    import src.application.services.http_tool_factory_service as mod
    monkeypatch.setattr(mod, "Toolkit", DummyToolkit)

    factory = HttpToolFactory()
    tools = [make_tool(HttpMethod.GET)]

    # RequestError
    with patch("httpx.request", side_effect=httpx.RequestError("net")):
        agno_tools = factory.create_tools_from_configs(tools)
        fn = agno_tools[0]._functions["t1"][0]
        msg = fn(id={"id": 1})
        assert "Erro na requisição" in msg

    # HTTPStatusError
    with patch("httpx.request") as req:
        resp = Mock(status_code=400, text="bad", content=b"bad", headers={})
        err = httpx.HTTPStatusError("bad", request=Mock(), response=resp)
        def raise_status():
            raise err
        resp.raise_for_status = raise_status
        req.return_value = resp

        agno_tools = factory.create_tools_from_configs(tools)
        fn = agno_tools[0]._functions["t1"][0]
        msg = fn(id={"id": 1})
        assert "Erro HTTP" in msg
