"""Colocar um banco Firebird offline e de volta online.

Necessário para a troca blue-green: renomear o arquivo .fdb só é seguro com o
servidor tendo largado o arquivo, e no Windows a renomeação simplesmente falha
enquanto houver handle aberto.

Dois caminhos, nesta ordem:

1. A Services API do firebird-driver, que é o caminho nativo.
2. O executável `gfix`, que acompanha toda instalação do Firebird.

O fallback existe porque a Services API do driver falhou de forma reprodutível
neste ambiente (Firebird 3.0.14 no Windows), com "Shared memory area is
probably already created by another engine instance in another Windows
session" — sintoma de o `connect_server` atingir um engine local/embedded em
vez do serviço que está de fato rodando. O mesmo `gfix -shut single -force 0`,
falando com o serviço, funciona. Como a troca de produção não pode depender
de um caminho que falha, ela cai para o gfix em vez de abortar.
"""

import logging

from firebird.driver import DatabaseError, OnlineMode, ShutdownMethod, ShutdownMode

from src.db import gfix
from src.db.config import FirebirdConfig
from src.db.connection import conectar_servidor

logger = logging.getLogger(__name__)

# Segundos que o servidor espera as conexões saírem antes de derrubá-las.
# 0 = imediato, que é o que se quer numa janela de troca.
TIMEOUT_SHUTDOWN = 0


def desligar(cfg: FirebirdConfig, database: str) -> None:
    """Derruba as conexões e deixa o banco offline, liberando o arquivo."""
    try:
        with conectar_servidor(cfg) as svc:
            svc.database.shutdown(
                database=database,
                mode=ShutdownMode.FULL,
                method=ShutdownMethod.FORCED,
                timeout=TIMEOUT_SHUTDOWN,
            )
        logger.info("Banco offline: %s", database)
        return
    except (DatabaseError, OSError) as e:
        logger.debug("Services API não desligou %s (%s) — tentando gfix", database, e)

    gfix.executar(cfg, "-shut", "full", "-force", str(TIMEOUT_SHUTDOWN), database=database)
    logger.info("Banco offline via gfix: %s", database)


def religar(cfg: FirebirdConfig, database: str) -> None:
    """Devolve o banco ao ar."""
    try:
        with conectar_servidor(cfg) as svc:
            svc.database.bring_online(database=database, mode=OnlineMode.NORMAL)
        logger.info("Banco online: %s", database)
        return
    except (DatabaseError, OSError) as e:
        logger.debug("Services API não religou %s (%s) — tentando gfix", database, e)

    gfix.executar(cfg, "-online", database=database)
    logger.info("Banco online via gfix: %s", database)
