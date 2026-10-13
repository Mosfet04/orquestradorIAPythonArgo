"""F2-01: ``ModelConfig`` neutro (imutável, validado, sem segredo em repr nem em erro).

``AgentConfig``/``TeamConfig``/``RagConfig`` expõem ``model_config`` derivado dos campos do
documento (``factory_ia_model`` + ``model`` e os opcionais ``model_params``, ``base_url``,
``api_key_ref``).
"""

from __future__ import annotations

import copy
import dataclasses
import pickle
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig, ModelParams, parse_secret_ref
from src.domain.entities.rag_config import RagConfig
from src.domain.entities.team_config import TeamConfig

MARKER = "sk-VALOR-QUE-NAO-PODE-APARECER"


def _agent(**overrides: Any) -> AgentConfig:
    fields: dict[str, Any] = {
        "id": "a",
        "nome": "Agente",
        "factory_ia_model": "ollama",
        "model": "llama3.2:latest",
        "descricao": "d",
        "prompt": "p",
    }
    fields.update(overrides)
    return AgentConfig(**fields)


def _team(**overrides: Any) -> TeamConfig:
    fields: dict[str, Any] = {
        "id": "t",
        "nome": "Time",
        "factory_ia_model": "openai",
        "model": "gpt-4o",
        "member_ids": ["a"],
    }
    fields.update(overrides)
    return TeamConfig(**fields)


class TestModelConfigValido:
    def test_minimo_tem_defaults_vazios(self) -> None:
        cfg = ModelConfig(provider="ollama", model_id="llama3.2:latest")

        assert (cfg.provider, cfg.model_id, dict(cfg.params), cfg.base_url, cfg.api_key_ref) == (
            "ollama",
            "llama3.2:latest",
            {},
            None,
            None,
        )

    def test_completo(self) -> None:
        cfg = ModelConfig(
            provider="openai_compatible",
            model_id="qwen2.5",
            params={"temperature": 0.2, "max_tokens": 512, "stream": True, "reasoning_effort": "low"},
            base_url="https://llm.example.invalid:8443/v1",
            api_key_ref="env:LLM_API_KEY",
        )

        assert dict(cfg.params) == {"temperature": 0.2, "max_tokens": 512, "stream": True, "reasoning_effort": "low"}
        assert cfg.base_url == "https://llm.example.invalid:8443/v1"
        assert cfg.api_key_ref == "env:LLM_API_KEY"

    @pytest.mark.parametrize("url", ["http://localhost:11434", "https://api.example.invalid/v1/", "http://[::1]:8000"])
    def test_base_url_http_ou_https(self, url: str) -> None:
        assert ModelConfig(provider="p", model_id="m", base_url=url).base_url == url

    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("env:OPENAI_API_KEY", ("env", "OPENAI_API_KEY")),
            ("env:AZURE_OPENAI_2_API_KEY", ("env", "AZURE_OPENAI_2_API_KEY")),
            ("file:/run/secrets/openai", ("file", "/run/secrets/openai")),
        ],
    )
    def test_referencias_validas(self, ref: str, expected: tuple[str, str]) -> None:
        assert parse_secret_ref(ref) == expected
        assert ModelConfig(provider="p", model_id="m", api_key_ref=ref).api_key_ref == ref


class TestModelConfigInvalido:
    @pytest.mark.parametrize("value", ["", None, 1, ["ollama"]])
    def test_provider_e_model_id_exigem_texto(self, value: Any) -> None:
        with pytest.raises(ValueError, match="Provider do modelo"):
            ModelConfig(provider=value, model_id="m")
        with pytest.raises(ValueError, match="ID do modelo"):
            ModelConfig(provider="p", model_id=value)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://llm.example.invalid",
            "llm.example.invalid:8080",
            "https://",
            f"https://user:{MARKER}@llm.example.invalid",
            f"https://{MARKER}@llm.example.invalid",
            f"https://llm.example.invalid/v1?api_key={MARKER}",
            f"https://llm.example.invalid/v1#{MARKER}",
            f"https://llm.example.invalid/{MARKER} x",
            f"https://llm.example.invalid/\n{MARKER}",
            f"https://llm.example.invalid:{MARKER}/v1",
            "http://[::1",
            123,
            f"http://llm.example.invalid\\{MARKER}",
            f"http://llm.example.invalid/v1\\{MARKER}",
            "http://%6cocalhost/v1",
            "http://\uff4cocalhost/v1",
            "http://bücher.example/v1",
            "http://llm.example.invalid:0/v1",
            "http://llm.example.invalid:/v1",
            "http://[::1]:/v1",
            pytest.param("http://llm.example.invalid/" + "a" * 2048, id="longa-demais"),
        ],
    )
    def test_base_url_invalida_sem_ecoar_o_valor(self, url: Any) -> None:
        with pytest.raises(ValueError, match="base_url") as exc_info:
            ModelConfig(provider="p", model_id="m", base_url=url)
        assert MARKER not in str(exc_info.value)
        assert exc_info.value.__cause__ is None

    @pytest.mark.parametrize(
        "ref",
        [
            MARKER,
            f"Bearer {MARKER}",
            f"env:{MARKER}",
            "env:",
            "env:1ABC",
            "file:",
            f"file:relativo/{MARKER}",
            "file:/run/secrets/a\x00b",
            f"vault:{MARKER}",
            "ENV:OPENAI_API_KEY",
            123,
        ],
    )
    def test_api_key_ref_invalido_sem_ecoar_o_valor(self, ref: Any) -> None:
        with pytest.raises(ValueError, match="api_key_ref") as exc_info:
            ModelConfig(provider="p", model_id="m", api_key_ref=ref)
        assert MARKER not in str(exc_info.value)

    @pytest.mark.parametrize(
        "params",
        [
            ["temperature"],
            "temperature=0.2",
            {1: "x"},
            {"": 1},
            {"temperature": None},
            {"stop": ["\n"]},
            {"extra": {"api_key": MARKER}},
        ],
    )
    def test_params_so_aceita_chave_texto_e_valor_escalar(self, params: Any) -> None:
        with pytest.raises(ValueError, match="model_params") as exc_info:
            ModelConfig(provider="p", model_id="m", params=params)
        assert MARKER not in str(exc_info.value)

    def test_params_invalido_nao_ecoa_a_chave(self) -> None:
        with pytest.raises(ValueError, match="model_params") as exc_info:
            ModelConfig(provider="p", model_id="m", params={MARKER: None})
        assert MARKER not in str(exc_info.value)

    @pytest.mark.parametrize(
        "name",
        [
            "API_KEY_RUN",
            "API_KEY_ADMIN",
            "MONGO_CONNECTION_STRING",
            "AWS_SECRET_ACCESS_KEY",
            "HOME",
            "API_KEY",
            "_API_KEY",
            "openai_api_key",
            "OPENAI_API_KEY_OLD",
            "1_API_KEY",
        ],
    )
    def test_env_so_aceita_variaveis_de_chave_de_provider(self, name: str) -> None:
        """Quem grava config não escolhe qualquer variável do processo: só ``<PROVEDOR>_API_KEY``."""
        with pytest.raises(ValueError, match="_API_KEY") as exc_info:
            parse_secret_ref(f"env:{name}")
        with pytest.raises(ValueError) as generic:
            parse_secret_ref("env:X")
        assert str(exc_info.value) == str(generic.value)  # mesma mensagem: o nome não é ecoado
        with pytest.raises(ValueError, match="api_key_ref"):
            ModelConfig(provider="p", model_id="m", api_key_ref=f"env:{name}")

    @pytest.mark.parametrize("items", [["a"], {"a": None}, {"": 1}, {1: "x"}, {"a": [MARKER]}])
    def test_model_params_valida_no_construtor(self, items: Any) -> None:
        with pytest.raises(ValueError, match="model_params") as exc_info:
            ModelParams(items)
        assert MARKER not in str(exc_info.value)

    def test_params_none_vale_como_vazio(self) -> None:
        assert ModelConfig(provider="p", model_id="m", params=None) == ModelConfig(provider="p", model_id="m")


class TestModelConfigImutavel:
    def test_campos_nao_mudam(self) -> None:
        cfg = ModelConfig(provider="p", model_id="m")
        with pytest.raises(FrozenInstanceError):
            cfg.model_id = "outro"  # type: ignore[misc]

    def test_params_e_copia_somente_leitura(self) -> None:
        source = {"temperature": 0.2}
        cfg = ModelConfig(provider="p", model_id="m", params=source)

        source["temperature"] = 1.0
        with pytest.raises(TypeError):
            cfg.params["temperature"] = 1.0  # type: ignore[index]
        assert dict(cfg.params) == {"temperature": 0.2}

    def test_igualdade_e_hash_por_valor(self) -> None:
        a = ModelConfig(provider="p", model_id="m", params={"top_p": 0.9}, api_key_ref="env:K_API_KEY")
        b = ModelConfig(provider="p", model_id="m", params={"top_p": 0.9}, api_key_ref="env:K_API_KEY")

        assert a == b
        assert hash(a) == hash(b)
        assert a != ModelConfig(provider="p", model_id="m", params={"top_p": 0.5}, api_key_ref="env:K_API_KEY")

    def test_copia_serializacao_e_asdict(self) -> None:
        cfg = ModelConfig(
            provider="p",
            model_id="m",
            params={"temperature": 0.2, "stream": True},
            base_url="https://llm.example.invalid/v1",
            api_key_ref="env:K_API_KEY",
        )

        assert copy.deepcopy(cfg) == cfg
        assert repr(cfg.params) == "ModelParams({'temperature': 0.2, 'stream': True})"
        assert pickle.loads(pickle.dumps(cfg)) == cfg  # noqa: S301 - objeto criado no próprio teste
        assert dataclasses.replace(cfg, model_id="z").params == cfg.params
        as_dict = dataclasses.asdict(cfg)
        assert as_dict["provider"] == "p" and dict(as_dict["params"]) == {"temperature": 0.2, "stream": True}

    def test_repr_sem_valores_de_params_nem_referencia(self) -> None:
        cfg = ModelConfig(
            provider="p",
            model_id="m",
            params={"seed": MARKER},
            base_url="https://llm.example.invalid/v1",
            api_key_ref="env:SEGREDO_DA_REF_API_KEY",
        )

        text = repr(cfg)
        assert MARKER not in text
        assert "SEGREDO_DA_REF" not in text
        assert "provider='p'" in text and "model_id='m'" in text


class TestEntidadesExpoemModelConfig:
    def test_agente_legado_gera_o_mesmo_provider_e_model_id(self) -> None:
        assert _agent().model_config == ModelConfig(provider="ollama", model_id="llama3.2:latest")

    def test_agente_com_campos_novos(self) -> None:
        config = _agent(
            model_params={"temperature": 0.1},
            base_url="http://vllm.internal:8000/v1",
            api_key_ref="file:/run/secrets/vllm",
        )

        assert config.model_config == ModelConfig(
            provider="ollama",
            model_id="llama3.2:latest",
            params={"temperature": 0.1},
            base_url="http://vllm.internal:8000/v1",
            api_key_ref="file:/run/secrets/vllm",
        )

    def test_agente_com_api_key_ref_invalido_e_invalido(self) -> None:
        with pytest.raises(ValueError, match="api_key_ref") as exc_info:
            _agent(api_key_ref=MARKER)
        assert MARKER not in str(exc_info.value)

    def test_agente_mantem_validacao_legada_primeiro(self) -> None:
        with pytest.raises(ValueError, match="Modelo do agente não pode estar vazio"):
            _agent(model="")

    def test_team_legado_e_com_campos_novos(self) -> None:
        assert _team().model_config == ModelConfig(provider="openai", model_id="gpt-4o")
        gateway = {"base_url": "https://gw.example.invalid/v1", "api_key_ref": "env:GW_API_KEY"}
        assert _team(**gateway).model_config == ModelConfig(provider="openai", model_id="gpt-4o", **gateway)
        with pytest.raises(ValueError, match="model_params"):
            _team(model_params={"temperature": [0.1]})

    def test_rag_legado(self) -> None:
        rag = RagConfig(active=True, model="nomic-embed-text:latest", factory_ia_model="ollama")

        assert rag.model_config == ModelConfig(provider="ollama", model_id="nomic-embed-text:latest")
        assert rag.embedder_model_config() == rag.model_config

    @pytest.mark.parametrize(
        ("model", "factory", "fallback"),
        [
            (None, None, ("ollama", "nomic-embed-text:latest")),
            ("", "gemini", ("gemini", "nomic-embed-text:latest")),
            ("embed-v1", "", ("ollama", "embed-v1")),
        ],
    )
    def test_rag_sem_modelo_completo_nao_tem_model_config_e_mantem_o_fallback(
        self, model: str | None, factory: str | None, fallback: tuple[str, str]
    ) -> None:
        rag = RagConfig(active=True, model=model, factory_ia_model=factory)

        assert rag.model_config is None
        embedder = rag.embedder_model_config()
        assert (embedder.provider, embedder.model_id) == fallback

    def test_rag_com_campos_novos(self) -> None:
        rag = RagConfig(
            active=True,
            model="text-embedding-3-small",
            factory_ia_model="openai_compatible",
            model_params={"dimensions": 256},
            base_url="https://emb.example.invalid/v1",
            api_key_ref="env:EMB_API_KEY",
        )

        assert rag.model_config == ModelConfig(
            provider="openai_compatible",
            model_id="text-embedding-3-small",
            params={"dimensions": 256},
            base_url="https://emb.example.invalid/v1",
            api_key_ref="env:EMB_API_KEY",
        )

    def test_rag_com_campos_novos_sem_modelo_e_invalido(self) -> None:
        with pytest.raises(ValueError, match="RAG"):
            RagConfig(active=True, model=None, factory_ia_model="ollama", api_key_ref="env:EMB_API_KEY")

    def test_rag_com_base_url_invalida_e_invalido(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            RagConfig(active=True, model="m", factory_ia_model="ollama", base_url="ftp://x")
