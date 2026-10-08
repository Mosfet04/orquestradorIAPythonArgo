# Python 3.12: mesma versão em que os locks são gerados e testada na CI (3.11 e 3.12).
# Tag com patch fixo; atualizar junto com a matriz da CI.
FROM python:3.12.15-slim-trixie

WORKDIR /app

# APP_HOST=0.0.0.0 só dentro do container (fora dele o default do AppConfig é 127.0.0.1).
# ENVIRONMENT=production: docs desligados e log JSON por padrão na imagem; o
# docker-compose.dev.yml troca para development.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=7777 \
    ENVIRONMENT=production

# Dependências só pelo lock de runtime, com hash. Todo pacote do lock tem wheel para
# Linux x86_64/aarch64 (cp312), então a imagem não precisa de compilador.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock

# Usuário sem privilégios. O código fica com dono root (somente leitura para o app).
# setup_structlog só faz mkdir de logs/ no cwd (nada grava lá hoje): o diretório já
# existe na imagem, então o container funciona com read_only (docker-compose.yml).
# UID/GID numéricos fixos: orquestradores com runAsNonRoot só verificam o número.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser
COPY . .
RUN mkdir -p logs && chown 10001:10001 logs
USER 10001:10001

EXPOSE 7777

# Sem curl na imagem: health via stdlib. /livez é público e não depende do MongoDB
# (o /admin/health responde 503 com dependência fora e vai exigir chave admin no F1-04).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('APP_PORT', '7777') + '/livez', timeout=5)"

CMD ["python", "app.py"]
