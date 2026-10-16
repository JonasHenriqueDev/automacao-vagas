# automacao-vagas

Busca vagas remotas periodicamente, calcula o match com o currículo mestre, gera um CV personalizado (PDF) para as
compatíveis e mostra tudo num painel de revisão. A candidatura final é sempre manual (o plano do projeto fica só localmente).

Fases implementadas: **1 (MVP)**, **2 (LLM e CV)** e **3 (painel)**.

## O que o sistema faz

- **Só vagas novas:** vagas abertas há mais de 24 h (configurável) nem são gravadas; a idade vem da data de
  **criação** da vaga (uma vaga antiga que recebeu um comentário hoje continua sendo velha). Duplicadas (mesma URL ou
  mesma empresa + título vindos de outra fonte) são ignoradas.
- **Painel** (`python -m app servir`): lista por vaga mais nova, com "aberta há X h", botão **Vaga original ↗**,
  CV, Aplicar e Descartar; candidaturas; configurações; integrações; saúde.
- **Execuções ao vivo:** "Rodar agora" abre uma tela com **barra de progresso** e o **log completo** da rodada
  (coleta, filtros, score, chamadas de LLM, CV, Discord), com download do `.log`.
- **Histórico das LLMs** (Integrações → log completo): cada chamada com provedor, modelo, tarefa, vaga, execução,
  resultado, HTTP, tempo, tokens e o erro inteiro; filtros e CSV.
- **CV em PDF** com hiperlinks clicáveis (e-mail, telefone, LinkedIn, GitHub) com a mesma aparência do texto.

## Início rápido (desenvolvimento)

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Linux: .venv/bin/pip
cp config.example.yaml config.yaml      # opcional
cp .env.example .env                    # preencha as chaves que tiver
python -m app gerar-chave               # cole CHAVE_MESTRA e SEGREDO_SESSAO no .env
python -m app criar-usuario             # usuário e senha do painel
python -m app rodar                     # uma rodada agora
python -m app listar                    # vagas compatíveis no terminal
python -m app servir                    # painel (127.0.0.1:8090) + agendador
python -m pytest                        # testes
```

Comandos: `rodar`, `listar`, `agendar` (só o agendador), `servir` (painel + agendador), `criar-usuario`,
`gerar-chave`, `cron`.

## Dados

- `data/curriculo_mestre.yaml`: fonte única do CV, **fora do git**. Copie de `data/curriculo_mestre.example.yaml`
  (dados fictícios) e preencha com o seu histórico; `nivel` das skills é estimativa.
- `data/dados_pessoais.yaml`: **fora do git**, nunca enviado às LLMs (há teste automatizado para isso).
  Copie de `data/dados_pessoais.example.yaml`. Os testes usam só os arquivos de exemplo.
- `config.yaml` e `.env`: fora do git. Prioridade: painel (banco) > `config.yaml` > padrão.

## Deploy (VM)

Arquivos em `deploy/`: `vagas.service` (systemd com `MemoryMax=250M`, `CPUQuota=50%`), `Caddyfile.bloco`
(bloco do painel, sem tocar nos do OpenSpeak) e `deploy.sh`. Para atualizar a VM:

```bash
ssh ubuntu@<vm> 'cd /opt/automacao-vagas && sudo -u vagas git pull --ff-only' # ou:
ssh ubuntu@<vm> '/opt/automacao-vagas/deploy/deploy.sh'                       # fetch + reset + pip + migrações + restart
```

Painel: https://vagasautomacao.duckdns.org (também em https://vagas.136-248-73-254.sslip.io).
