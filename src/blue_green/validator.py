"""Validação da base recém-construída, antes de promovê-la a produção.

A promoção é irreversível na prática (o banco antigo é descartado logo depois),
então nada é promovido sem antes provar que está inteiro: todas as tabelas
existem, nenhuma está vazia e todos os índices foram criados.
"""

import logging
from dataclasses import dataclass, field

from firebird.driver import DatabaseError

from src.blue_green.constants import EXPECTED_INDEXES, EXPECTED_TABLES, MAY_BE_EMPTY
from src.db import manage, schema
from src.db.config import FirebirdConfig, load_config
from src.db.connection import conectar

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    is_valid: bool
    erro_conexao: str | None = None
    missing_tables: list[str] = field(default_factory=list)
    empty_tables: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    contagens: dict[str, int] = field(default_factory=dict)
    duplicadas: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.is_valid:
            total = sum(self.contagens.values())
            return f"VÁLIDA — {total:,} linhas em {len(self.contagens)} tabelas, pronta para troca"
        if self.erro_conexao:
            return f"INVÁLIDA — não foi possível abrir a base: {self.erro_conexao}"
        problemas = []
        if self.missing_tables:
            problemas.append(f"tabelas ausentes: {', '.join(self.missing_tables)}")
        if self.empty_tables:
            problemas.append(f"tabelas vazias: {', '.join(self.empty_tables)}")
        if self.missing_indexes:
            problemas.append(f"índices ausentes: {', '.join(self.missing_indexes)}")
        if self.duplicadas:
            det = ", ".join(f"{t}: {n:,}" for t, n in self.duplicadas.items())
            problemas.append(f"chave repetida (carga possivelmente duplicada) — {det}")
        return "INVÁLIDA — " + "; ".join(problemas)


# Fração de chaves repetidas a partir da qual a carga é considerada duplicada.
#
# Aqui o piso observado é ZERO: medido sobre a competência 2610 inteira, não
# existe uma única chave natural repetida em nenhuma das 14 tabelas que
# declaram chave. Isso é o oposto do CNPJ, onde a Receita publica chave
# repetida de verdade e o molde precisa de folga para o ruído da fonte.
#
# Com piso zero, o limiar pode ser apertado: 0,1% ainda deixa margem para uma
# competência futura trazer alguma repetição legítima, e continua 1.000x abaixo
# do que um arquivo carregado duas vezes produziria (100%, já que aqui é um
# arquivo por tabela — não há partição).
LIMIAR_DUPLICADAS = 0.001


def _chave_repetida(con, contagens: dict[str, int]) -> dict[str, int]:
    """Detecta carga duplicada pela fração de chave natural repetida.

    É a razão de `db/dedup.py` não existir neste projeto e esta checagem sim.
    O molde tem os dois porque lá a fonte publica chave repetida de verdade e
    ela precisa ser REMOVIDA. Aqui a fonte é limpa, então não há o que remover
    — mas continua havendo o que vigiar: carregar o mesmo arquivo duas vezes,
    que um `--continuar` malfeito ou um checkpoint estragado produziriam.

    Repetição abaixo do limiar passa e sai como aviso no log, para não sumir
    de vista se a fonte mudar de comportamento numa competência futura.
    """
    achados: dict[str, int] = {}
    cur = con.cursor()

    for tabela, chave in schema.CHAVES_NATURAIS.items():
        total = contagens.get(tabela) or 0
        if not total:
            continue
        cols = ", ".join(chave)
        # As colunas da chave vão nomeadas no SELECT interno: o Firebird recusa
        # tabela derivada com coluna sem nome ("no column name specified for
        # column number 1 in derived table"), então `SELECT 1` não serve aqui.
        cur.execute(
            f"SELECT COUNT(*) FROM (SELECT {cols} FROM {tabela} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1) d"
        )
        n = int(cur.fetchone()[0] or 0)
        if not n:
            continue

        fracao = n / total
        if fracao >= LIMIAR_DUPLICADAS:
            achados[tabela] = n
        else:
            logger.warning(
                "%s: %d chave(s) repetida(s) em %s linhas (%.6f%%) — abaixo do "
                "limiar de %.1f%%, tratado como ruído da fonte",
                tabela, n, f"{total:,}", fracao * 100, LIMIAR_DUPLICADAS * 100,
            )

    return achados


def validar(cfg: FirebirdConfig | None = None, database: str | None = None) -> ValidationResult:
    """Valida a base em `database` (padrão: a staging derivada da config)."""
    cfg = cfg or load_config()
    alvo = database or cfg.staging_database

    # Aponta a config para a base a validar sem alterar a original.
    cfg_alvo = FirebirdConfig(**{**cfg.__dict__, "database": alvo})

    # Só a abertura entra neste try: envolver a inspeção inteira faria um erro
    # de SQL na checagem sair como "não foi possível abrir a base", que manda
    # quem for depurar para o lado errado.
    try:
        gerenciador = conectar(cfg_alvo)
        con = gerenciador.__enter__()
    except DatabaseError as e:
        return ValidationResult(
            is_valid=False,
            erro_conexao=str(e).splitlines()[0],
            missing_tables=EXPECTED_TABLES[:],
            missing_indexes=EXPECTED_INDEXES[:],
        )

    try:
        faltando: list[str] = []
        vazias: list[str] = []
        contagens: dict[str, int] = {}

        for tabela in EXPECTED_TABLES:
            if not manage.tabela_existe(con, tabela):
                faltando.append(tabela)
                continue
            n = manage.contar(con, tabela)
            contagens[tabela] = n
            if n == 0 and tabela not in MAY_BE_EMPTY:
                vazias.append(tabela)

        sem_indice = [
            nome for nome in EXPECTED_INDEXES if not manage.indice_existe(con, nome)
        ]

        duplicadas = _chave_repetida(con, contagens)

        return ValidationResult(
            is_valid=not (faltando or vazias or sem_indice or duplicadas),
            missing_tables=faltando,
            empty_tables=vazias,
            missing_indexes=sem_indice,
            contagens=contagens,
            duplicadas=duplicadas,
        )
    finally:
        gerenciador.__exit__(None, None, None)
