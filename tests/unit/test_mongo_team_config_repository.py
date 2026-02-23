"""Testes para MongoTeamConfigRepository._map_to_entity."""

from __future__ import annotations

from src.infrastructure.repositories.mongo_team_config_repository import (
    MongoTeamConfigRepository,
)


class TestMapToEntity:
    """Testa o mapeamento de documentos Mongo para TeamConfig."""

    def test_map_with_snake_case_fields(self):
        doc = {
            "id": "team-1",
            "nome": "Team Router",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "mode": "route",
            "member_ids": ["a1", "a2"],
            "user_memory_active": True,
            "summary_active": False,
            "active": True,
            "descricao": "Descricao",
            "prompt": "Prompt de sistema",
        }
        entity = MongoTeamConfigRepository._map_to_entity(doc)
        assert entity.id == "team-1"
        assert entity.nome == "Team Router"
        assert entity.factory_ia_model == "ollama"
        assert entity.member_ids == ["a1", "a2"]
        assert entity.user_memory_active is True
        assert entity.summary_active is False
        assert entity.descricao == "Descricao"
        assert entity.prompt == "Prompt de sistema"

    def test_map_with_camel_case_fields(self):
        doc = {
            "id": "team-2",
            "nome": "Team Coord",
            "model": "gpt-4o",
            "factoryIaModel": "openai",
            "mode": "coordinate",
            "memberIds": ["x1", "x2", "x3"],
            "userMemoryActive": False,
            "summaryActive": True,
            "active": True,
        }
        entity = MongoTeamConfigRepository._map_to_entity(doc)
        assert entity.factory_ia_model == "openai"
        assert entity.member_ids == ["x1", "x2", "x3"]
        assert entity.user_memory_active is False
        assert entity.summary_active is True

    def test_map_with_defaults(self):
        doc = {
            "id": "team-3",
            "nome": "Team Min",
            "model": "llama3:latest",
            "factoryIaModel": "ollama",
            "memberIds": ["m1"],
        }
        entity = MongoTeamConfigRepository._map_to_entity(doc)
        assert entity.mode == "route"
        assert entity.user_memory_active is True
        assert entity.summary_active is False
        assert entity.active is True
        assert entity.descricao is None
        assert entity.prompt is None

    def test_map_snake_case_takes_precedence(self):
        """Quando ambos snake_case e camelCase existem, snake_case vence."""
        doc = {
            "id": "team-4",
            "nome": "Team Prec",
            "model": "m",
            "factory_ia_model": "ollama",
            "factoryIaModel": "openai",
            "member_ids": ["a"],
            "memberIds": ["b", "c"],
        }
        entity = MongoTeamConfigRepository._map_to_entity(doc)
        assert entity.factory_ia_model == "ollama"
        assert entity.member_ids == ["a"]
