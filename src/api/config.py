"""Configuração da API, lida do ambiente.

Os três caminhos são volumes distintos no pod, com ciclos de vida
independentes: a base é trocada todo mês, as credenciais são permanentes e o
log tem janela de retenção. Manter isso explícito aqui evita que alguém
encoste os três no mesmo lugar por conveniência.

A configuração do BANCO também mora aqui, e isso é decisão
-----------------------------------------------------------
Antes, `ConfigAPI` cobria só metade do que a aplicação precisa — JWT, volumes,
site — e a outra metade (`DB_NAME`, `FB_EMBEDDED`, ...) era lida direto do
`os.environ` lá no fundo, na PRIMEIRA consulta. Isso criava uma dependência que
nenhuma assinatura declarava: alguém precisava ter carregado o `.env` antes, e
quem fazia isso variava conforme a rota chamada primeiro.

O sintoma era bom de guardar: `criar_app` funcionava se a primeira requisição
fosse de CEP (porque `consulta/cep.py` é também CLI e faz `load_dotenv` no
import) e devolvia 500 se fosse a listagem (porque `consulta/listagem.py` não
é CLI e não faz). O comportamento da aplicação dependia da ORDEM das rotas, por
efeito colateral de import.

Agora a configuração inteira é montada de uma vez, no ambiente, por quem chama
`do_ambiente()` — e `criar_app(cfg)` passa a ser autossuficiente. `load_dotenv`
vira assunto exclusivo dos entrypoints (`api/servir.py`), que é onde sempre
deveria ter estado.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..db.config import FirebirdConfig, load_config


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
    # Como abrir a base. `None` significa "esta configuração não serve para
    # consultar" — é o caso legítimo do /manager, que só fala com SQLite.
    # `publica.criar_app` recusa `None` quando vai abrir o Firebird de verdade,
    # em vez de deixar o erro aparecer na primeira requisição.
    firebird: FirebirdConfig | None = None

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

        # A mesma leitura de ambiente que o ETL usa, feita AGORA e não na
        # primeira consulta. `DB_NAME` vazio é o sintoma de ambiente não
        # carregado, e sem esta guarda ele viraria um DSN `inet://localhost:3050/`
        # e o erro do driver seria "Your user name and password are not
        # defined" — que manda procurar administrador e senha onde não há
        # servidor, porque a base é embedded num arquivo ao lado.
        fb = load_config()
        if not fb.database:
            raise RuntimeError(
                "DB_NAME não definido — a API não sabe qual .fdb abrir. "
                "No pod isto vem do Dockerfile; fora dele, do .env carregado "
                "pelo entrypoint (api/servir.py)"
            )

        return cls(
            credenciais=creds / "credenciais.db",
            logs=logs,
            estatisticas=creds / "estatisticas.db",
            jwt_segredo=segredo,
            validade_token_s=int(_env("API_VALIDADE_TOKEN_S", "3600")),
            retencao_dias=int(_env("API_RETENCAO_DIAS", "0")),
            site_dir=_env("API_SITE_DIR", ""),
            firebird=fb,
        )
