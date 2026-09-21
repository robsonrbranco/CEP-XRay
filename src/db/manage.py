"""Criação e inspeção de tabelas e índices no Firebird 3.0.

O Firebird não tem `CREATE TABLE IF NOT EXISTS` nem `DROP TABLE IF EXISTS`, e
não expõe information_schema — a introspecção é feita nas tabelas de sistema
RDB$RELATIONS e RDB$INDICES. Os nomes de objeto criados sem aspas ficam
gravados em MAIÚSCULAS e preenchidos com espaço à direita, daí o strip/upper
em toda comparação.
"""

import logging

from firebird.driver import DatabaseError

from . import schema

logger = logging.getLogger(__name__)


def tabela_existe(con, nome: str) -> bool:
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM RDB$RELATIONS WHERE TRIM(RDB$RELATION_NAME) = ?",
        [nome.upper()],
    )
    return cur.fetchone() is not None


def indice_existe(con, nome: str) -> bool:
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM RDB$INDICES WHERE TRIM(RDB$INDEX_NAME) = ?",
        [nome.upper()],
    )
    return cur.fetchone() is not None


def dropar_tabela(con, nome: str) -> None:
    if not tabela_existe(con, nome):
        return
    cur = con.cursor()
    cur.execute(f"DROP TABLE {nome}")
    con.commit()
    logger.info("Tabela %s removida", nome)


def contar(con, nome: str) -> int:
    cur = con.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {nome}")
    return cur.fetchone()[0]


def criar_tabelas(con, preservar: set[str] | None = None) -> None:
    """(Re)cria as tabelas do schema.

    Tabelas em `preservar` — aquelas que o checkpoint indica já carregadas —
    não são dropadas, para que uma execução interrompida possa continuar de
    onde parou.
    """
    preservar = preservar or set()

    for tabela in schema.TABLES:
        if tabela in preservar:
            logger.info("Tabela '%s' preservada (checkpoint indica dado carregado)", tabela)
            continue
        dropar_tabela(con, tabela)
        cur = con.cursor()
        cur.execute(schema.create_table_sql(tabela))
        con.commit()

    logger.info("Tabelas configuradas")


def criar_indices(con) -> None:
    """Cria os índices — sempre DEPOIS da carga.

    Índice ativo durante INSERT em massa é o maior custo de uma carga Firebird:
    cada linha inserida atualiza cada índice da tabela. Construir tudo no fim,
    com a tabela já povoada, é substancialmente mais rápido.
    """
    for nome in schema.INDEXES:
        if indice_existe(con, nome):
            logger.info("Índice %s já existe", nome)
            continue
        sql = schema.create_index_sql(nome)
        logger.info("Criando índice %s ...", nome)
        try:
            cur = con.cursor()
            cur.execute(sql)
            con.commit()
        except DatabaseError as e:
            con.rollback()
            logger.error("Falha ao criar índice %s: %s", nome, e)
            raise

    logger.info("Índices criados")


# ---------------------------------------------------------------------------
# Proveniência
# ---------------------------------------------------------------------------

def criar_metadados(con) -> None:
    """Cria a tabela de proveniência, se ainda não existir."""
    if tabela_existe(con, schema.METADADOS):
        return
    cur = con.cursor()
    cur.execute(schema.CREATE_METADADOS)
    con.commit()
    logger.info("Tabela %s criada", schema.METADADOS)


def gravar_metadados(con, dados: dict[str, object]) -> None:
    """Regrava a proveniência inteira.

    Apaga e reinsere em vez de atualizar chave a chave: a tabela é escrita uma
    vez por carga, e substituir o conjunto todo evita sobrar chave de uma carga
    anterior que a nova não produz mais.
    """
    criar_metadados(con)
    cur = con.cursor()
    cur.execute(f"DELETE FROM {schema.METADADOS}")
    for chave, valor in dados.items():
        cur.execute(
            f"INSERT INTO {schema.METADADOS} (chave, valor) VALUES (?, ?)",
            (str(chave)[:40], str(valor)[:500]),
        )
    con.commit()
    logger.info("Proveniência gravada: %d chave(s)", len(dados))


def ler_metadados(con) -> dict[str, str]:
    """Devolve a proveniência, ou vazio se a base não a tiver.

    Base construída antes desta tabela existir devolve `{}` — e o chamador
    precisa tratar isso, porque a produção atual é uma dessas.
    """
    if not tabela_existe(con, schema.METADADOS):
        return {}
    cur = con.cursor()
    cur.execute(f"SELECT chave, valor FROM {schema.METADADOS}")
    return {c.strip(): (v or "").strip() for c, v in cur.fetchall()}
