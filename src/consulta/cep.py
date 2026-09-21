#!/usr/bin/env python3
"""Ficha completa de um CEP.

    python -m src.consulta.cep 01001000
    python -m src.consulta.cep 01001-000 --json

É a camada que a API envolve. Devolve um dicionário com o dado como está na
base — a tradução para o contrato dos Correios é feita em `api/correios.py`,
separada de propósito: aqui não há nome de campo de terceiro.

Dois caminhos, e o segundo é o que costuma faltar
-------------------------------------------------
1. **CEP de logradouro** — está em `logradouro`, com rua, bairro e município.
   São 1.720.964 dos CEPs da base.
2. **CEP que não é de logradouro** — não está em `logradouro` e mesmo assim é
   um CEP válido, porque cai numa FAIXA de município. É o CEP geral de cidade
   pequena, onde não há logradouro individualizado.

Sem o caminho 2 a API devolveria 404 para CEP que existe. A busca por faixa é
`faixa_ini <= cep <= faixa_fim` em `cidade_faixa`, comparação de texto sobre
`VARCHAR(8)` — que funciona porque CEP é zero-padded de largura fixa, e por
isso a ordem lexicográfica é a ordem numérica.

O que o caminho 2 afirma, e o que NÃO afirma
--------------------------------------------
Ele responde **"a que município este CEP pertence"**, que é informação real e
vem da faixa que a fonte publica. Ele **não** afirma que aquele CEP específico
está cadastrado.

Medido: as 5.571 faixas de município cobrem 91,6% dos 100 milhões de números de
8 dígitos. Então `99999999` devolve 200 com Muliterno/RS (é o topo da faixa do
Rio Grande do Sul) enquanto `00000000` e `20000000` devolvem 404. Um cliente que
precise saber se o CEP está de fato cadastrado deve olhar `tipoCEP`: **1 é CEP
com logradouro próprio; 2 é resolução por faixa de município**.

Não há como estreitar isso sem inventar regra. Restringir o fallback a algum
padrão de "CEP geral" seria criar um critério que a fonte não publica.

Todo JOIN é LEFT
----------------
Medido nesta competência: zero órfãos em `bairro_id`, `cidade_id` e
`distrito_id`. Ainda assim, `bairro_id` é **NULL em 111.700 linhas (6,5%)** e
`distrito_id` em 1.645.011 (96%) — um `INNER JOIN` faria a maioria dos CEPs
sumir da resposta.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.api.cep import CEPInvalido, normalizar  # noqa: E402
from src.db import connection  # noqa: E402

# Todo apelido é explícito: `logradouro.estado` guarda a SIGLA e
# `estado.estado` guarda o NOME por extenso. Sem apelido os dois chegam ao
# cursor com o mesmo rótulo, e o dicionário fica só com o último — a sigla
# sumiria em silêncio e a resposta traria "São Paulo" onde promete "SP".
SQL_LOGRADOURO = """
SELECT l.cep             AS cep,
       l.tipo            AS tipo,
       l.nome_logradouro AS nome_logradouro,
       l.estado          AS uf_sigla,
       l.latitude        AS latitude,
       l.longitude       AS longitude,
       l.cep_ativo       AS cep_ativo,
       b.id_bairro       AS id_bairro,
       b.bairro          AS bairro,
       c.id_cidade       AS id_cidade,
       c.cidade          AS cidade,
       c.cidade_ibge     AS cidade_ibge,
       c.ddd             AS ddd,
       d.id_distrito     AS id_distrito,
       d.distrito        AS distrito,
       e.estado          AS uf_nome,
       e.capital         AS uf_capital,
       e.regiao          AS uf_regiao
FROM logradouro l
LEFT JOIN bairro   b ON b.id_bairro   = l.bairro_id
LEFT JOIN cidade   c ON c.id_cidade   = l.cidade_id
LEFT JOIN distrito d ON d.id_distrito = l.distrito_id
LEFT JOIN estado   e ON e.sigla       = l.estado
WHERE l.cep = ?
"""

# O CEP que não tem logradouro próprio cai na faixa do município.
SQL_FAIXA = """
SELECT f.id_cidade   AS id_cidade,
       f.faixa_ini   AS faixa_ini,
       f.faixa_fim   AS faixa_fim,
       f.estado      AS uf_sigla,
       c.cidade      AS cidade,
       c.cidade_ibge AS cidade_ibge,
       c.ddd         AS ddd,
       c.latitude    AS latitude,
       c.longitude   AS longitude,
       e.estado      AS uf_nome,
       e.capital     AS uf_capital,
       e.regiao      AS uf_regiao
FROM cidade_faixa f
LEFT JOIN cidade c ON c.id_cidade = f.id_cidade
LEFT JOIN estado e ON e.sigla     = f.estado
WHERE ? BETWEEN f.faixa_ini AND f.faixa_fim
ROWS 1
"""


@contextmanager
def _conexao(con=None):
    """Usa a conexão emprestada, ou abre uma própria.

    Conexão emprestada NÃO é fechada aqui: quem emprestou continua dono dela.
    É o que permite a API manter uma conexão viva por processo (ver
    `api/conexao.py`) em vez de pagar ~390 ms de attach por requisição.
    """
    if con is not None:
        yield con
        return
    with connection.conectar() as propria:
        yield propria


def _um(cur, sql: str, params: list) -> dict | None:
    cur.execute(sql, params)
    linha = cur.fetchone()
    if linha is None:
        return None
    nomes = [d[0].lower() for d in cur.description]
    return dict(zip(nomes, linha))


def consultar(cep: str, con=None) -> dict | None:
    """Ficha do CEP, ou None se não existir nem como logradouro nem em faixa.

    `cep` já deve vir normalizado (8 dígitos). Quem recebe entrada de usuário
    chama `api.cep.normalizar` antes, para que forma errada vire 400 e não 404.
    """
    with _conexao(con) as c:
        cur = c.cursor()

        bruto = _um(cur, SQL_LOGRADOURO, [cep])
        if bruto is not None:
            ficha = {
                "cep": bruto["cep"],
                "origem": "logradouro",
                "tipo": bruto["tipo"],
                "nome_logradouro": bruto["nome_logradouro"],
                "estado": bruto["uf_sigla"],
                "latitude": bruto["latitude"],
                "longitude": bruto["longitude"],
                "cep_ativo": bruto["cep_ativo"],
                "bairro": _sub(bruto, "id_bairro", "bairro"),
                "distrito": _sub(bruto, "id_distrito", "distrito"),
                "cidade": _cidade(bruto),
                "uf": _uf(bruto),
            }
            cur.execute(
                "SELECT complemento FROM log_complemento WHERE cep = ?", [cep]
            )
            linha = cur.fetchone()
            ficha["complemento"] = linha[0] if linha else None

            cur.execute(
                "SELECT grandes_usuarios FROM log_grande_usuario WHERE cep = ?", [cep]
            )
            linha = cur.fetchone()
            ficha["grande_usuario"] = linha[0] if linha else None
            return ficha

        faixa = _um(cur, SQL_FAIXA, [cep])
        if faixa is None:
            return None
        return {
            "cep": cep,
            # A distinção importa para o contrato: este CEP não tem logradouro,
            # e é isso que faz `tipoCEP` sair como 2 em `api/correios.py`.
            "origem": "faixa",
            "tipo": None,
            "nome_logradouro": None,
            "estado": faixa["uf_sigla"],
            "latitude": faixa["latitude"],
            "longitude": faixa["longitude"],
            "cep_ativo": None,
            "bairro": None,
            "distrito": None,
            "cidade": _cidade(faixa),
            "uf": _uf(faixa),
            "complemento": None,
            "grande_usuario": None,
            "faixa": (faixa["faixa_ini"], faixa["faixa_fim"]),
        }


def _sub(bruto: dict, chave_id: str, chave_nome: str) -> dict | None:
    if bruto.get(chave_id) is None:
        return None
    return {"id": bruto[chave_id], "nome": bruto[chave_nome]}


def _cidade(bruto: dict) -> dict | None:
    if bruto.get("id_cidade") is None:
        return None
    return {
        "id": bruto["id_cidade"],
        "nome": bruto["cidade"],
        "ibge": bruto["cidade_ibge"],
        "ddd": bruto["ddd"],
    }


def _uf(bruto: dict) -> dict | None:
    if bruto.get("uf_nome") is None:
        return None
    return {
        "sigla": bruto.get("uf_sigla"),
        "nome": bruto["uf_nome"],
        "capital": bruto.get("uf_capital"),
        "regiao": bruto.get("uf_regiao"),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.consulta.cep", description="Ficha de um CEP"
    )
    p.add_argument("cep", help="com ou sem pontuação")
    p.add_argument("--json", action="store_true", help="saída em JSON")
    args = p.parse_args()

    try:
        cep = normalizar(args.cep)
    except CEPInvalido as e:
        print(f"CEP inválido: {e}", file=sys.stderr)
        return 2

    ficha = consultar(cep)
    if ficha is None:
        print(f"Nenhum registro para o CEP {cep}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(ficha, ensure_ascii=False, indent=2))
        return 0

    cidade = ficha["cidade"] or {}
    bairro = ficha["bairro"] or {}
    linha = " ".join(x for x in (ficha["tipo"], ficha["nome_logradouro"]) if x)
    print(f"CEP ............ {cep[:5]}-{cep[5:]}")
    if linha:
        print(f"Logradouro ..... {linha}")
    if bairro.get("nome"):
        print(f"Bairro ......... {bairro['nome']}")
    print(f"Município ...... {cidade.get('nome')}/{ficha['estado']}"
          f"  (IBGE {cidade.get('ibge')}, DDD {cidade.get('ddd')})")
    if ficha.get("complemento"):
        print(f"Complemento .... {ficha['complemento']}")
    if ficha.get("grande_usuario"):
        print(f"Grande usuário . {ficha['grande_usuario']}")
    if ficha.get("distrito"):
        print(f"Distrito ....... {ficha['distrito']['nome']}")
    if ficha.get("latitude") is not None:
        print(f"Coordenadas .... {ficha['latitude']}, {ficha['longitude']}")
    print(f"Origem ......... {ficha['origem']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
