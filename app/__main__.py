"""CLI: python -m app <comando>"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import sys
import threading

from cryptography.fernet import Fernet
from sqlalchemy import select

from .bootstrap import criar_servicos
from .db.models import Match, Vaga


def _log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for ruidoso in ("alembic", "httpx", "httpcore"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def cmd_rodar(args) -> int:
    from .pipeline import rodar

    sv = criar_servicos(args.config)
    ex = rodar(sv, origem="manual")
    if ex is None:
        print("Já existe uma execução em andamento.")
        return 1
    print(f"Execução #{ex.id}: {ex.vagas_coletadas} coletadas, {ex.vagas_novas} novas.")
    if ex.erros:
        print("Erros:\n" + ex.erros)
    return 0 if not ex.erros else 2


def cmd_listar(args) -> int:
    sv = criar_servicos(args.config)
    limite = float(sv.config.obter("match.limite", 70))
    with sv.fabrica() as s:
        ms = s.scalars(select(Match).where(Match.score >= (args.minimo if args.minimo is not None else limite))
                       .order_by(Match.score.desc()).limit(args.limite)).all()
        for m in ms:
            v = m.vaga
            det = json.loads(m.detalhamento_json or "{}")
            flag = " [preliminar]" if m.preliminar else ""
            print(f"{m.score:5.1f}%  {v.titulo} — {v.empresa or 'n/d'}  ({v.fonte}){flag}\n        {v.url}"
                  f"\n        bateu: {', '.join(det.get('bateram', []))}  | faltou: {', '.join(det.get('faltaram', []))}")
        if not ms:
            print("Nenhuma vaga compatível ainda.")
    return 0


def cmd_agendar(args) -> int:
    from .agendador import Agendador

    sv = criar_servicos(args.config)
    ag = Agendador(sv)
    ag.iniciar()
    print(f"Agendador ativo (cron '{ag.cron}'). Próxima execução: {ag.proxima_execucao()}")
    parar = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: parar.set())
    parar.wait()
    ag.parar()
    return 0


def cmd_servir(args) -> int:
    import uvicorn

    from .painel.app import criar_app

    sv = criar_servicos(args.config)
    host = sv.config.obter("painel.host", "127.0.0.1")
    porta = int(sv.config.obter("painel.porta", 8090))
    uvicorn.run(criar_app(sv), host=host, port=porta, log_level="info", proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0


def cmd_criar_usuario(args) -> int:
    from .painel.auth import definir_credenciais

    sv = criar_servicos(args.config)
    usuario = args.usuario or input("Usuário: ").strip()
    senha = getpass.getpass("Senha (mín. 12 caracteres): ")
    if senha != getpass.getpass("Repita a senha: "):
        print("As senhas não conferem.")
        return 1
    try:
        definir_credenciais(sv, usuario, senha)
    except ValueError as e:
        print(e)
        return 1
    print("Credenciais salvas.")
    return 0


def cmd_gerar_chave(_args) -> int:
    import secrets

    print("# Cole no .env da VM (chmod 600):")
    print(f"CHAVE_MESTRA={Fernet.generate_key().decode()}")
    print(f"SEGREDO_SESSAO={secrets.token_urlsafe(48)}")
    return 0


def cmd_proxima(args) -> int:
    from .agendador import Agendador, validar_cron

    sv = criar_servicos(args.config)
    ag = Agendador(sv)
    validar_cron(ag.cron, ag.fuso)
    print(f"Cron: {ag.cron} ({ag.fuso})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app", description="Automação de vagas")
    ap.add_argument("--config", help="caminho do config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rodar", help="executa uma rodada agora").set_defaults(f=cmd_rodar)
    p = sub.add_parser("listar", help="lista as vagas compatíveis")
    p.add_argument("--minimo", type=float)
    p.add_argument("--limite", type=int, default=30)
    p.set_defaults(f=cmd_listar)
    sub.add_parser("agendar", help="só o agendador (sem painel)").set_defaults(f=cmd_agendar)
    sub.add_parser("servir", help="painel + agendador no mesmo processo").set_defaults(f=cmd_servir)
    p = sub.add_parser("criar-usuario", help="define usuário e senha do painel")
    p.add_argument("--usuario")
    p.set_defaults(f=cmd_criar_usuario)
    sub.add_parser("gerar-chave", help="gera CHAVE_MESTRA e SEGREDO_SESSAO").set_defaults(f=cmd_gerar_chave)
    sub.add_parser("cron", help="valida o cron configurado").set_defaults(f=cmd_proxima)
    args = ap.parse_args(argv)
    _log()
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
