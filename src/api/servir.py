#!/usr/bin/env python3
"""Sobe a API.

    python -m src.api.servir                 pública, porta 8000
    python -m src.api.servir --manager       manager, porta 8001

**São dois processos, de propósito.** O `/manager` não pode conviver com a API
pública: uma credencial capaz de criar credenciais é escalada de privilégio, e
processos separados em portas separadas é a única barreira que não depende de
nenhum segredo estar correto.

A porta do manager **não deve ser exposta** para fora do cluster. Publicá-la
anula a decisão inteira — não há autenticação nela justamente para que ninguém
se convença de que expor é aceitável.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    p = argparse.ArgumentParser(prog="servir", description=__doc__.split("\n")[0])
    p.add_argument("--manager", action="store_true",
                   help="sobe o /manager em vez da API pública")
    p.add_argument("--host", default="127.0.0.1",
                   help="padrão 127.0.0.1; o manager não deveria sair daqui")
    p.add_argument("--porta", type=int)
    p.add_argument("--workers", type=int, default=1,
                   help="mais de um exige ServerMode=Classic no firebird.conf")
    args = p.parse_args()

    import uvicorn

    from .config import ConfigAPI

    try:
        cfg = ConfigAPI.do_ambiente()
    except RuntimeError as e:
        print(f"configuração incompleta: {e}", file=sys.stderr)
        return 2

    if args.manager:
        from .manager import criar_app
        app, porta = criar_app(cfg), args.porta or 8001
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"AVISO: manager em {args.host} — ele não deveria ser "
                  f"alcançável de fora do cluster", file=sys.stderr)
    else:
        from .publica import criar_app
        app, porta = criar_app(cfg), args.porta or 8000

    if args.workers > 1:
        # Cada worker é um processo com seu próprio attachment ao Firebird. Em
        # ServerMode=Super o primeiro toma o arquivo e os demais morrem no
        # attach -- medido, 1 de 4. Classic dá 4 de 4.
        print(f"{args.workers} workers: confirme ServerMode = Classic no "
              f"firebird.conf, senão só um sobe", file=sys.stderr)

    uvicorn.run(app, host=args.host, port=porta, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
