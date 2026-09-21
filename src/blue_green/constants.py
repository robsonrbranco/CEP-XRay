"""Objetos que uma base recém-construída precisa ter para ser promovida.

Deriva de db.schema em vez de repetir a lista: se uma tabela ou índice for
adicionado lá, a validação do blue-green passa a exigi-lo automaticamente.
"""

from src.db import schema

EXPECTED_TABLES: list[str] = list(schema.TABLES)
EXPECTED_INDEXES: list[str] = list(schema.INDEXES)

# Tabelas que podem legitimamente vir vazias numa competência.
#
# Nenhuma: as 15 tabelas da fonte sempre têm conteúdo, da menor (27 linhas em
# `estado`) à maior (1,72 M em `logradouro`). Vazia é sinal de carga
# interrompida, não de competência atípica.
MAY_BE_EMPTY: frozenset[str] = frozenset()
