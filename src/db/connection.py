"""Conexão, criação e tuning do banco Firebird 3.0.

O tuning de carga não é exposto em SQL pelo Firebird. Ele entra por dois
caminhos distintos, e a diferença importa:

* Na CRIAÇÃO do banco, via `DatabaseConfig` do firebird-driver: `page_size`,
  `db_cache_size` e `forced_writes` viram parâmetros do DPB. `page_size` só
  pode ser definido aqui — mudar depois exige backup/restore com gbak.
* Em banco JÁ EXISTENTE, via Services API (o equivalente programático do
  gfix): `set_write_mode` e `set_default_cache_size`.

Force Write OFF acelera muito a carga porque o servidor deixa de forçar fsync
a cada página gravada, mas enquanto estiver assim o banco não tem garantia de
durabilidade. Como a carga é reproduzível a partir dos arquivos da RFB, o risco
é aceitável durante o ETL e inaceitável depois dele — daí o par
modo_carga()/modo_producao().
"""

import logging
from contextlib import contextmanager

from firebird.driver import (
    DatabaseConfig,
    DatabaseError,
    DbAccessMode,
    DbSpaceReservation,
    DbWriteMode,
    connect,
    connect_server,
    driver_config,
    get_api,
)

from . import gfix
from .config import FirebirdConfig, load_config

logger = logging.getLogger(__name__)

# Nome sob o qual a configuração do banco fica registrada no driver.
CONFIG_NAME = "cepxray"


def registrar(cfg: FirebirdConfig | None = None, forced_writes: bool = True) -> str:
    """Registra (ou reconfigura) o banco no driver e devolve o nome do registro.

    `forced_writes` só tem efeito na criação do banco; para um banco existente
    use modo_carga()/modo_producao().
    """
    cfg = cfg or load_config()

    if cfg.client_library:
        driver_config.fb_client_library.value = cfg.client_library

    db = driver_config.get_database(CONFIG_NAME)
    if db is None:
        db = driver_config.register_database(CONFIG_NAME)

    db.dsn.value = cfg.dsn
    db.user.value = cfg.user
    db.password.value = cfg.password
    if cfg.embedded:
        # Sem isto o Dispatcher tentaria Remote primeiro. Embedded não
        # autentica, mas o usuário ainda decide privilégio: sem `user` a
        # conexão entra como o usuário do sistema operacional.
        db.config.value = "Providers = Engine12"
    db.charset.value = cfg.charset
    db.db_charset.value = cfg.charset
    db.page_size.value = cfg.page_size
    db.forced_writes.value = forced_writes
    if cfg.cache_pages:
        db.db_cache_size.value = cfg.cache_pages

    return CONFIG_NAME


@contextmanager
def conectar(cfg: FirebirdConfig | None = None):
    """Conexão com o banco configurado, fechada ao sair do bloco."""
    nome = registrar(cfg)
    con = connect(nome)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def conectar_servidor(cfg: FirebirdConfig | None = None):
    """Conexão com a Services API (gfix/gbak/gstat programáticos)."""
    cfg = cfg or load_config()
    if cfg.client_library:
        driver_config.fb_client_library.value = cfg.client_library
    svc = connect_server(
        f"inet://{cfg.host}:{cfg.port}",
        user=cfg.user,
        password=cfg.password,
    )
    try:
        yield svc
    finally:
        svc.close()


def database_exists(cfg: FirebirdConfig | None = None) -> bool:
    try:
        with conectar(cfg):
            return True
    except DatabaseError:
        return False


def create_database_sql(cfg: FirebirdConfig) -> str:
    """Monta o CREATE DATABASE, com charset E collation no nível do banco."""
    senha = cfg.password.replace("'", "''")
    return (
        f"CREATE DATABASE '{cfg.dsn}'\n"
        f"  USER '{cfg.user}' PASSWORD '{senha}'\n"
        f"  PAGE_SIZE {cfg.page_size}\n"
        f"  DEFAULT CHARACTER SET {cfg.charset} COLLATION {cfg.collation}"
    )


def create_if_not_exists(cfg: FirebirdConfig | None = None) -> bool:
    """Cria o banco se ainda não existir. Devolve True se criou agora.

    Executa o DDL literal em vez de usar o `create_database()` do driver: o
    helper aceita `db_charset`, mas não expõe a COLLATION padrão do banco, e
    ela é definível apenas na criação. Sem ela, `ORDER BY razao_social` joga
    todo nome iniciado por acento para depois do Z.

    O page_size também só pode ser definido aqui — mudar depois exige gbak.
    Force Write e cache ficam para modo_carga(), que roda logo em seguida.
    """
    cfg = cfg or load_config()

    if database_exists(cfg):
        logger.info("Banco já existe: %s", cfg.database)
        return False

    registrar(cfg, forced_writes=False)
    sql = create_database_sql(cfg)
    logger.info(
        "Criando banco %s (page_size=%d, charset=%s, collation=%s)",
        cfg.database, cfg.page_size, cfg.charset, cfg.collation,
    )

    api = get_api()
    att = api.util.execute_create_database(sql, 3)  # dialeto 3
    att.detach()

    # Antes de qualquer página de dado ser escrita.
    modo_espaco_cheio(cfg)
    return True


def modo_espaco_cheio(cfg: FirebirdConfig | None = None) -> None:
    """Desliga a reserva de espaço nas páginas de dados.

    Por padrão o Firebird deixa parte de cada página de dados livre para
    guardar versões futuras do registro. Isso serve a uma base que recebe
    UPDATE — e esta não recebe: nasce por carga em massa, nunca é alterada e
    termina read-only. Cada byte reservado é desperdício permanente,
    multiplicado por 220 milhões de linhas.

    Medido numa amostra de 120 mil linhas de estabelecimento, com índices:
    38,3 MB com reserva contra 34,0 MB sem, ou seja 11,3% menos. Nos 35 GB da
    base isso são cerca de 4 GB.

    Diferente do Force Write, isto NÃO é revertido em modo_producao(): a
    ausência de reserva é uma propriedade permanente desta base, não um ajuste
    de carga. E só vale para páginas escritas DEPOIS da mudança — por isso é
    aplicado na criação do banco, antes de existir qualquer dado.
    """
    cfg = cfg or load_config()
    try:
        with conectar_servidor(cfg) as svc:
            svc.database.set_space_reservation(
                database=cfg.database, mode=DbSpaceReservation.USE_FULL
            )
    except (DatabaseError, OSError) as e:
        logger.debug("Services API não ajustou a reserva (%s) — usando gfix", e)
        gfix.executar(cfg, "-use", "full")
    logger.info("Reserva de espaço = USE_FULL (sem espaço ocioso por página)")


def _set_write_mode(cfg: FirebirdConfig, mode: DbWriteMode, rotulo: str) -> None:
    try:
        with conectar_servidor(cfg) as svc:
            svc.database.set_write_mode(database=cfg.database, mode=mode)
        logger.info("Force Write = %s", rotulo)
    except DatabaseError as e:
        logger.warning("Não foi possível ajustar Force Write (%s): %s", rotulo, e)


def modo_carga(cfg: FirebirdConfig | None = None) -> None:
    """Prepara um banco existente para carga em massa.

    Force Write ASYNC + cache grande. Os índices NÃO entram aqui: são criados
    depois da carga, por manage.criar_indices().
    """
    cfg = cfg or load_config()
    _set_write_mode(cfg, DbWriteMode.ASYNC, "ASYNC (carga)")

    if cfg.cache_pages:
        try:
            with conectar_servidor(cfg) as svc:
                svc.database.set_default_cache_size(
                    database=cfg.database, size=cfg.cache_pages
                )
            logger.info(
                "Cache do banco = %s páginas (~%d MB)", f"{cfg.cache_pages:,}", cfg.cache_mb
            )
        except DatabaseError as e:
            logger.warning("Não foi possível ajustar o cache: %s", e)


def modo_producao(cfg: FirebirdConfig | None = None) -> None:
    """Devolve o banco ao modo seguro depois da carga."""
    cfg = cfg or load_config()
    _set_write_mode(cfg, DbWriteMode.SYNC, "SYNC (produção)")


def _set_access_mode(cfg: FirebirdConfig, mode: DbAccessMode, rotulo: str,
                     database: str | None = None) -> None:
    alvo = database or cfg.database
    with conectar_servidor(cfg) as svc:
        svc.database.set_access_mode(database=alvo, mode=mode)
    logger.info("Modo de acesso de %s = %s", alvo, rotulo)


def modo_somente_leitura(cfg: FirebirdConfig | None = None, database: str | None = None) -> None:
    """Marca o banco como somente leitura no próprio header.

    A base em produção é base de consulta: ninguém altera dado nela, porque a
    atualização mensal substitui o arquivo inteiro. Deixar isso apenas como
    combinado operacional é frágil — um UPDATE acidental de um cliente
    corromperia a base até a próxima carga. Em read-only o engine recusa
    qualquer escrita, e como bônus para de manter versionamento de registro e
    varredura de páginas sujas numa base que nunca muda.

    Precisa ser desfeito (modo_leitura_escrita) para carregar de novo — mas o
    ETL nunca recarrega por cima: constrói outro arquivo.
    """
    cfg = cfg or load_config()
    _set_access_mode(cfg, DbAccessMode.READ_ONLY, "somente leitura", database)


def modo_leitura_escrita(cfg: FirebirdConfig | None = None, database: str | None = None) -> None:
    """Devolve o banco ao modo gravável."""
    cfg = cfg or load_config()
    _set_access_mode(cfg, DbAccessMode.READ_WRITE, "leitura e escrita", database)


def estatisticas(cfg: FirebirdConfig | None = None) -> None:
    """Recalcula a seletividade dos índices.

    O otimizador do Firebird usa a seletividade gravada quando o índice foi
    criado. Depois de uma carga grande ela fica defasada e o plano escolhido
    piora — daí a recomputação ao fim do ETL.
    """
    with conectar(cfg) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT TRIM(RDB$INDEX_NAME) FROM RDB$INDICES "
            "WHERE RDB$SYSTEM_FLAG = 0 AND COALESCE(RDB$INDEX_INACTIVE, 0) = 0"
        )
        nomes = [r[0] for r in cur.fetchall()]
        for nome in nomes:
            cur.execute(f"SET STATISTICS INDEX {nome}")
        con.commit()
        logger.info("Seletividade recalculada para %d índices", len(nomes))
