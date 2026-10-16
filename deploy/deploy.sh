#!/usr/bin/env bash
# Deploy na VM. Rodar com um usuário que tenha sudo (ex.: ubuntu), a partir de qualquer pasta:
#   ./deploy/deploy.sh            # atualiza para origin/main
#   ./deploy/deploy.sh v0.2.0     # ou para uma tag/commit
# O app vive em /opt/automacao-vagas e roda como o usuário 'vagas'. Não mexe no OpenSpeak nem no Caddy.
set -euo pipefail

APP=/opt/automacao-vagas
REPO=https://github.com/JonasHenriqueDev/automacao-vagas.git   # repositório público: sem credenciais
REF="${1:-origin/main}"

como_vagas() { sudo -u vagas -H bash -c "cd $APP && $1"; }

# Primeira vez: transforma a pasta (instalada antes por pacote) em um clone. Arquivos ignorados pelo git
# (.env, data/dados_pessoais.yaml, banco, saída, .venv) ficam intactos.
if [ ! -d "$APP/.git" ]; then
  como_vagas "git init -q -b main && git remote add origin $REPO"
fi

# Espera uma rodada em andamento terminar (até 10 min) para não reiniciar o serviço no meio dela.
for _ in $(seq 1 60); do
  rodando=$(como_vagas ".venv/bin/python -c \"import sqlite3; c=sqlite3.connect('data/vagas.db'); print(c.execute(\\\"select count(*) from execucoes where status='rodando' and fim is null\\\").fetchone()[0])\"" 2>/dev/null || echo 0)
  [ "$rodando" = "0" ] && break
  echo "Rodada em andamento; aguardando..."; sleep 10
done

como_vagas "git fetch --tags --quiet origin && git reset --hard $REF"
como_vagas "[ -d .venv ] || python3 -m venv .venv"
como_vagas ".venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -e ."
# Migrações do banco (Alembic)
como_vagas ".venv/bin/python -c \"from app.config import Config; from app.db.sessao import migrar; c = Config.carregar(); migrar(c.caminho('banco'))\""
como_vagas "chmod 600 .env data/dados_pessoais.yaml 2>/dev/null || true"

sudo systemctl restart vagas
sleep 6
systemctl is-active vagas
curl -fsS -m 10 http://127.0.0.1:8090/healthz && echo
como_vagas "git log --oneline -1"
