"""Testes de contrato: a MESMA suíte roda contra toda implementação de uma porta.

Cada suíte recebe, por fixture parametrizada, uma fábrica ``seed -> implementação``.
Hoje só as implementações em memória de ``tests/fakes`` rodam aqui (elas são a base dos
demais testes, então precisam obedecer ao contrato). Adapter que dependa de serviço
externo (ex.: Mongo) entra como novo parâmetro marcado ``live``/``integration``.

Todo teste deste diretório recebe o marker ``contract`` (ver ``tests/conftest.py``).
"""
