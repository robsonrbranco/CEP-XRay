"""Chamada ao binário `gfix`, usado como alternativa à Services API do driver.

A Services API do firebird-driver falha de forma reprodutível neste ambiente
(Firebird 3.0.14 no Windows) com "Shared memory area is probably already
created by another engine instance in another Windows session" ou com erro de
CreateFile — sintoma de o `connect_server` atingir um engine local em vez do
serviço que está de fato rodando. As mesmas operações via `gfix`, que fala com
o serviço, funcionam.

Este módulo existe para que esse contorno fique num lugar só. Quem precisar de
uma operação de manutenção tenta a Services API e cai para cá.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import FirebirdConfig

logger = logging.getLogger(__name__)


def binario(cfg: FirebirdConfig) -> str | None:
    """Localiza o gfix: ao lado do fbclient configurado, ou no PATH."""
    if cfg.client_library:
        candidato = Path(cfg.client_library).parent / (
            "gfix.exe" if os.name == "nt" else "gfix"
        )
        if candidato.exists():
            return str(candidato)
    return shutil.which("gfix")


def executar(cfg: FirebirdConfig, *args: str, database: str | None = None) -> None:
    """Roda `gfix <args> <host>:<database>`. Levanta RuntimeError se falhar."""
    exe = binario(cfg)
    if not exe:
        raise RuntimeError(
            "gfix não encontrado — instale as ferramentas do Firebird ou aponte "
            "FB_CLIENT_LIBRARY para o diretório da instalação"
        )
    alvo = f"{cfg.host}:{database or cfg.database}"
    r = subprocess.run(
        [exe, *args, "-user", cfg.user, "-password", cfg.password, alvo],
        capture_output=True,
        text=True,
    )
    saida = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or saida:
        raise RuntimeError(f"gfix {' '.join(args)} falhou: {saida or r.returncode}")
    logger.debug("gfix %s em %s: ok", " ".join(args), alvo)
