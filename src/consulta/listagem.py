"""Consultas de listagem: UFs, localidades, bairros e endereços filtrados.

Separado de `consulta/cep.py` porque o perfil é outro: lá é uma linha por
chave indexada, aqui é varredura com filtro e paginação. O que os dois têm em
comum é a disciplina — `LEFT JOIN` sempre, e o nome do campo da fonte, nunca o
do contrato de terceiro (a tradução é de `api/correios.py`).

Sobre a busca por nome
----------------------
`WHERE cidade = ?` compara com a collation `WIN_PTBR`, que ignora acento e
caixa: `'sao paulo'` acha `'São Paulo'`. É por isso que não há coluna `_SA` no
schema, e é por isso que estas consultas não normalizam nada antes de comparar.

O que NÃO dá para fazer: busca por trecho no meio do nome. O Firebird 3.0 não
tem equivalente a trigrama, e `LIKE '%texto%'` varreria 1,72 milhão de linhas
sem usar índice. `LIKE 'texto%'` (prefixo) usa o índice e é o que as rotas de
listagem oferecem.
"""

from __future__ import annotations

# Máximo de itens por página. Teto, não sugestão: sem ele um cliente pedindo
# `size=1000000` faria o pod montar uma lista de 1,7 milhão de dicionários em
# memória para serializar de uma vez.
TAMANHO_MAXIMO = 200
TAMANHO_PADRAO = 50


def _linhas(cur) -> list[dict]:
    nomes = [d[0].lower() for d in cur.description]
    return [dict(zip(nomes, linha)) for linha in cur.fetchall()]


def _contar(cur, sql: str, params: list) -> int:
    cur.execute(sql, params)
    return int(cur.fetchone()[0] or 0)


def limitar(tamanho: int | None) -> int:
    if not tamanho or tamanho < 1:
        return TAMANHO_PADRAO
    return min(tamanho, TAMANHO_MAXIMO)


def ufs(con) -> list[dict]:
    """As 27 UFs, cada uma com todas as suas faixas.

    Duas consultas em vez de um JOIN: são 27 e 64 linhas, e juntá-las faria a
    UF repetir por faixa, obrigando quem consome a desagrupar.
    """
    cur = con.cursor()
    cur.execute(
        "SELECT sigla, estado, capital, regiao, faixa_ini, faixa_fim, "
        "latitude, longitude FROM estado ORDER BY sigla"
    )
    linhas = _linhas(cur)

    cur.execute(
        "SELECT sigla, faixa_ini, faixa_fim FROM estado_faixa "
        "ORDER BY sigla, faixa_ini"
    )
    por_uf: dict[str, list[dict]] = {}
    for f in _linhas(cur):
        por_uf.setdefault(f["sigla"], []).append(
            {"faixaInicial": f["faixa_ini"], "faixaFinal": f["faixa_fim"]}
        )

    for linha in linhas:
        linha["faixas"] = por_uf.get(linha["sigla"], [])
    return linhas


def uf(con, sigla: str) -> dict | None:
    achado = [u for u in ufs(con) if (u["sigla"] or "").upper() == sigla.upper()]
    return achado[0] if achado else None


def localidades(
    con, uf: str | None = None, nome: str | None = None,
    pagina: int = 0, tamanho: int = TAMANHO_PADRAO,
) -> tuple[list[dict], int]:
    tamanho = limitar(tamanho)
    onde, params = [], []
    if uf:
        onde.append("c.estado = ?")
        params.append(uf.upper())
    if nome:
        onde.append("c.cidade STARTING WITH ?")
        params.append(nome)
    filtro = ("WHERE " + " AND ".join(onde)) if onde else ""

    cur = con.cursor()
    total = _contar(cur, f"SELECT COUNT(*) FROM cidade c {filtro}", params)
    cur.execute(
        f"""SELECT c.id_cidade   AS id_cidade,
                   c.cidade      AS cidade,
                   c.estado      AS estado,
                   c.cidade_ibge AS cidade_ibge,
                   c.ddd         AS ddd,
                   c.latitude    AS latitude,
                   c.longitude   AS longitude,
                   f.faixa_ini   AS faixa_ini,
                   f.faixa_fim   AS faixa_fim
            FROM cidade c
            LEFT JOIN cidade_faixa f ON f.id_cidade = c.id_cidade
            {filtro}
            ORDER BY c.estado, c.cidade
            ROWS ? TO ?""",
        [*params, pagina * tamanho + 1, (pagina + 1) * tamanho],
    )
    return _linhas(cur), total


def bairros(
    con, uf: str, id_cidade: int, pagina: int = 0, tamanho: int = TAMANHO_PADRAO
) -> tuple[list[dict], int]:
    tamanho = limitar(tamanho)
    params = [uf.upper(), id_cidade]
    cur = con.cursor()
    total = _contar(
        cur,
        "SELECT COUNT(*) FROM bairro WHERE estado = ? AND cidade_id = ?",
        params,
    )
    cur.execute(
        """SELECT id_bairro, bairro, estado, cidade_id, latitude, longitude
           FROM bairro
           WHERE estado = ? AND cidade_id = ?
           ORDER BY bairro
           ROWS ? TO ?""",
        [*params, pagina * tamanho + 1, (pagina + 1) * tamanho],
    )
    return _linhas(cur), total


def enderecos(
    con, uf: str | None = None, id_localidade: int | None = None,
    bairro: str | None = None, logradouro: str | None = None,
    pagina: int = 0, tamanho: int = TAMANHO_PADRAO,
) -> tuple[list[dict], int]:
    """Listagem paginada de endereços.

    Exige pelo menos um filtro: sem ele a resposta seria uma paginação sobre
    1,72 milhão de linhas, e a primeira página não diria nada a ninguém.
    """
    tamanho = limitar(tamanho)
    onde, params = [], []
    if uf:
        onde.append("l.estado = ?")
        params.append(uf.upper())
    if id_localidade:
        onde.append("l.cidade_id = ?")
        params.append(id_localidade)
    if logradouro:
        onde.append("l.nome_logradouro STARTING WITH ?")
        params.append(logradouro)
    if bairro:
        onde.append("b.bairro = ?")
        params.append(bairro)
    if not onde:
        raise ValueError("informe ao menos um filtro: uf, localidade, bairro ou logradouro")

    filtro = "WHERE " + " AND ".join(onde)
    juncao = "LEFT JOIN bairro b ON b.id_bairro = l.bairro_id"

    cur = con.cursor()
    total = _contar(
        cur, f"SELECT COUNT(*) FROM logradouro l {juncao} {filtro}", params
    )
    cur.execute(
        f"""SELECT l.cep             AS cep,
                   l.tipo            AS tipo,
                   l.nome_logradouro AS nome_logradouro,
                   l.estado          AS uf_sigla,
                   b.bairro          AS bairro,
                   c.cidade          AS cidade,
                   c.id_cidade       AS id_cidade,
                   c.cidade_ibge     AS cidade_ibge,
                   c.ddd             AS ddd,
                   x.complemento     AS complemento,
                   g.grandes_usuarios AS grande_usuario
            FROM logradouro l
            {juncao}
            LEFT JOIN cidade c ON c.id_cidade = l.cidade_id
            LEFT JOIN log_complemento x ON x.cep = l.cep
            LEFT JOIN log_grande_usuario g ON g.cep = l.cep
            {filtro}
            ORDER BY l.cep
            ROWS ? TO ?""",
        [*params, pagina * tamanho + 1, (pagina + 1) * tamanho],
    )
    linhas = _linhas(cur)
    # A mesma forma que `consulta/cep.consultar` devolve, para `api/correios`
    # ter um único formato de entrada.
    fichas = [
        {
            "cep": r["cep"],
            "origem": "logradouro",
            "tipo": r["tipo"],
            "nome_logradouro": r["nome_logradouro"],
            "estado": r["uf_sigla"],
            "bairro": {"nome": r["bairro"]} if r["bairro"] else None,
            "cidade": {
                "id": r["id_cidade"], "nome": r["cidade"],
                "ibge": r["cidade_ibge"], "ddd": r["ddd"],
            },
            "complemento": r["complemento"],
            "grande_usuario": r["grande_usuario"],
        }
        for r in linhas
    ]
    return fichas, total


def atualizacao(con) -> dict:
    """`GET /cep/v1/atualizacao` — a competência que este pod está servindo."""
    from src.db import manage

    md = manage.ler_metadados(con)
    linhas = md.get("linhas_total")
    return {
        "competencia": md.get("competencia"),
        "atualizadoEm": md.get("construida_em"),
        "totalRegistros": int(linhas) if linhas and linhas.isdigit() else None,
    }
