"""Configuração da API, lida do ambiente.

Os três caminhos são volumes distintos no pod, com ciclos de vida
independentes: a base é trocada todo mês, as credenciais são permanentes e o
log tem janela de retenção. Manter isso explícito aqui evita que alguém
encoste os três no mesmo lugar por conveniência.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(nome: str, padrao: str = "") -> str:
    v = os.getenv(nome)
    return v if v not in (None, "") else padrao


@dataclass
class ConfigAPI:
    # Volume de credenciais: permanente, sobrevive à troca mensal da base.
    credenciais: Path
    # Volume de log: janela de retenção, escrita a cada requisição.
    logs: Path
    estatisticas: Path
    # Segredo de assinatura do JWT. Sem valor padrão de propósito: um padrão
    # aqui seria uma chave conhecida assinando tokens de produção.
    jwt_segredo: str
    validade_token_s: int = 3600
    # 0 = nunca podar. O analítico é mantido indefinidamente por decisão
    # explícita: nada apaga log sozinho. `/manager/manutencao/podar` continua
    # existindo, mas exige o número de dias na chamada.
    retencao_dias: int = 0
    # Pasta do site compilado pelo Hugo, servido em `/`. Vazio significa não
    # montar nada — é o caso fora da imagem, onde a API roda sem página.
    site_dir: str = ""

    @classmethod
    def do_ambiente(cls) -> "ConfigAPI":
        segredo = _env("API_JWT_SEGREDO")
        if not segredo:
            raise RuntimeError(
                "API_JWT_SEGREDO não definido — sem ele os tokens seriam "
                "assinados com uma chave previsível"
            )
        creds = Path(_env("API_CREDENCIAIS_DIR", "./creds"))
        logs = Path(_env("API_LOGS_DIR", "./logs-api"))
        return cls(
            credenciais=creds / "credenciais.db",
            logs=logs,
            estatisticas=creds / "estatisticas.db",
            jwt_segredo=segredo,
            validade_token_s=int(_env("API_VALIDADE_TOKEN_S", "3600")),
            retencao_dias=int(_env("API_RETENCAO_DIAS", "0")),
            site_dir=_env("API_SITE_DIR", ""),
        )
