"""Troca da base de produção pela base recém-construída.

O ETL nunca altera a base em produção: ele constrói uma base nova do zero em
`<nome>_staging.fdb` e, só no fim, esta troca coloca a nova no lugar. Em
Firebird isso é renomeação de arquivo — não existe ALTER DATABASE RENAME, e
não é preciso derrubar um banco que continua existindo.

    producao.fdb          -> producao_old.fdb
    producao_staging.fdb  -> producao.fdb
    producao_old.fdb      -> descartado

A ordem importa. O `_old` existe para que, se a segunda renomeação falhar, a
base anterior ainda esteja em disco e possa ser devolvida — e é justamente o
que `_restaurar()` faz. Só depois de a nova estar no lugar e online é que o
`_old` é descartado.

Quem renomeia é este processo, não o servidor: os arquivos precisam estar
visíveis para ele. Quando o Firebird roda em container ou noutra máquina,
`FB_LOCAL_DATA_DIR` faz a ponte entre o caminho do servidor e o do cliente.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from src.blue_green import manutencao
from src.blue_green.state import StateManager
from src.blue_green.validator import ValidationResult, validar
from src.db.config import FirebirdConfig, load_config

logger = logging.getLogger(__name__)


@dataclass
class SwitchResult:
    success: bool
    message: str
    source_month: str | None = None
    validacao: ValidationResult | None = None


class BlueGreenSwitcher:
    def __init__(self, cfg: FirebirdConfig | None = None, state: StateManager | None = None):
        self._cfg = cfg or load_config()
        self._state = state or StateManager()

    # -- caminhos, do ponto de vista de quem renomeia --------------------

    def _local(self, database: str) -> Path:
        return self._cfg.caminho_local(database)

    def switch(self, force: bool = False) -> SwitchResult:
        cfg = self._cfg
        ativo, staging, old = cfg.database, cfg.staging_database, cfg.old_database

        validacao = None
        if not force:
            validacao = validar(cfg, staging)
            if not validacao.is_valid:
                return SwitchResult(False, validacao.summary, validacao=validacao)

        f_ativo, f_staging, f_old = self._local(ativo), self._local(staging), self._local(old)

        if not f_staging.exists():
            return SwitchResult(
                False,
                f"Base nova não encontrada em {f_staging} — rode o ETL antes da troca",
            )

        # Sobra de uma troca anterior interrompida: descartar antes, senão o
        # rename do ativo esbarra num arquivo já existente.
        if f_old.exists():
            logger.warning("Descartando %s remanescente de troca anterior", f_old)
            manutencao.desligar(cfg, old)
            f_old.unlink()

        tinha_ativo = f_ativo.exists()

        try:
            manutencao.desligar(cfg, staging)

            if tinha_ativo:
                manutencao.desligar(cfg, ativo)
                f_ativo.rename(f_old)
                logger.info("Base anterior preservada em %s", f_old)

            f_staging.rename(f_ativo)
            manutencao.religar(cfg, ativo)

        except Exception as e:
            restaurado = self._restaurar(f_ativo, f_old, f_staging, tinha_ativo)
            return SwitchResult(
                False,
                f"Erro durante a troca: {e}. {restaurado}",
                validacao=validacao,
            )

        # Estado antes do descarte: se apagar o _old falhar, os metadados da
        # troca já estão gravados e a produção já está correta.
        self._state.promote_staging()
        info = self._state.get_active() or {}

        if f_old.exists():
            try:
                f_old.unlink()
                logger.info("Base anterior descartada")
            except OSError as e:
                logger.warning("Base anterior não pôde ser removida (%s): %s", f_old, e)

        return SwitchResult(
            True,
            f"Troca concluída — {f_ativo.name} agora tem os dados novos",
            source_month=info.get("source_month"),
            validacao=validacao,
        )

    def _restaurar(self, f_ativo: Path, f_old: Path, f_staging: Path, tinha_ativo: bool) -> str:
        """Tenta desfazer uma troca que falhou no meio."""
        if not tinha_ativo:
            return "Não havia base anterior para restaurar."
        if f_ativo.exists():
            return "A base de produção está no lugar."
        if f_old.exists():
            try:
                f_old.rename(f_ativo)
                manutencao.religar(self._cfg, self._cfg.database)
                return "A base anterior foi restaurada."
            except OSError as e:
                return (
                    f"ATENÇÃO: a base anterior está em {f_old} e não pôde ser "
                    f"restaurada ({e}) — renomeie manualmente para {f_ativo}."
                )
        return f"ATENÇÃO: nenhuma base em {f_ativo}; verifique {f_staging} e {f_old}."

    def descartar_antiga(self) -> str:
        """Remove a base `_old` deixada por uma troca interrompida."""
        f_old = self._local(self._cfg.old_database)
        if not f_old.exists():
            return f"{f_old.name} não existe — nada a fazer"
        manutencao.desligar(self._cfg, self._cfg.old_database)
        f_old.unlink()
        return f"{f_old.name} removida"
