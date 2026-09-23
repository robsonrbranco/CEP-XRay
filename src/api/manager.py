"""`/manager` — gestão de credenciais e estatísticas de uso.

**Aplicação separada, porta separada, não exposta.** Não é organização de
código: é a barreira de segurança. Uma credencial capaz de emitir credenciais é
escalada de privilégio por desenho, então o `/manager` não pode conviver com a
API pública nem compartilhar autenticação com ela.

Separar por processo é a única proteção que não depende de nenhum segredo estar
correto — se a porta não é alcançável, não há o que atacar. Quem publicar esta
aplicação na mesma porta da API pública, ou expô-la para fora do cluster, anula
a decisão inteira.

Por isso não há autenticação aqui: ela seria um segundo mecanismo dependendo de
um segredo, e daria a falsa impressão de que expor a porta é aceitável. Se um
dia for preciso defesa em profundidade, a credencial administrativa entra
**além** do isolamento de rede, nunca no lugar dele.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import esquemas
from . import token as mod_token
from .config import ConfigAPI
from .credenciais import Credenciais, CredencialNaoEncontrada, STATUS_VALIDOS
from .uso import Estatisticas


class NovaCredencial(BaseModel):
    contratante_nome: str = Field(min_length=1)
    contratante_documento: str | None = None
    contratante_email: str | None = None
    quota_mensal: int | None = Field(default=None, ge=0)
    observacao: str | None = None


class AlteracaoCredencial(BaseModel):
    contratante_nome: str | None = Field(default=None, min_length=1)
    contratante_documento: str | None = None
    contratante_email: str | None = None
    quota_mensal: int | None = Field(default=None, ge=0)
    observacao: str | None = None
    status: str | None = None


DESCRICAO = """Gestão de credenciais e estatísticas de uso. **Não faz parte do contrato do
SERPRO** — esta é a parte nossa.

**Esta aplicação não deve ser exposta.** Ela roda em processo e porta
separados da API pública, e não tem autenticação própria de propósito: uma
credencial capaz de criar credenciais seria escalada de privilégio, e um
segundo mecanismo baseado em segredo daria a falsa impressão de que publicar a
porta é aceitável. O isolamento de rede é a única proteção que não depende de
nenhum segredo estar correto.

Os erros aqui saem como `{"detail": ...}`, e não `{"message": ...}` como na
API pública — lá o formato imita o SERPRO, aqui é nosso.
"""


def criar_app(cfg: ConfigAPI) -> FastAPI:
    app = FastAPI(
        title="CEP-XRay — manager",
        version="1.0",
        description=DESCRICAO,
    )
    app.state.credenciais = Credenciais(cfg.credenciais)
    app.state.estatisticas = Estatisticas(cfg.estatisticas)
    app.state.cfg = cfg

    def _cred(request: Request, chave: str):
        try:
            return request.app.state.credenciais.obter(chave)
        except CredencialNaoEncontrada:
            raise HTTPException(404, "credencial não encontrada") from None

    # -- credenciais ----------------------------------------------------

    @app.get(
        "/manager/credenciais",
        response_model=esquemas.ListaCredenciais,
        responses=esquemas.ENTRADA_INVALIDA,
        summary="Lista as credenciais",
        tags=["credenciais"],
    )
    async def listar(request: Request, status: str | None = None):
        if status and status not in STATUS_VALIDOS:
            raise HTTPException(400, f"status inválido: {status}")
        creds = request.app.state.credenciais.listar(status)
        return {"credenciais": [c.para_json() for c in creds]}

    @app.post(
        "/manager/credenciais",
        status_code=201,
        response_model=esquemas.CredencialCriada,
        summary="Inclui uma credencial",
        description="Devolve o `consumerSecret` **uma única vez**. Depois "
                    "disto só o hash existe, e não há como recuperá-lo.",
        tags=["credenciais"],
    )
    async def incluir(request: Request, dados: NovaCredencial):
        cred, secret = request.app.state.credenciais.criar(**dados.model_dump())
        # O segredo aparece UMA vez. Depois disto só existe o hash, e não há
        # como recuperá-lo -- só rotacionar.
        return {**cred.para_json(), "consumerSecret": secret,
                "aviso": "guarde o consumerSecret: ele não será exibido de novo"}

    @app.get(
        "/manager/credenciais/{chave}",
        response_model=esquemas.CredencialComConsumo,
        responses=esquemas.NAO_ENCONTRADA,
        summary="Consulta uma credencial",
        tags=["credenciais"],
    )
    async def consultar(request: Request, chave: str):
        cred = _cred(request, chave)
        usado = request.app.state.estatisticas.consumo_do_mes(chave)
        return {**cred.para_json(), "consumoDoMes": usado}

    @app.patch(
        "/manager/credenciais/{chave}",
        response_model=esquemas.Credencial,
        responses={**esquemas.NAO_ENCONTRADA, **esquemas.ENTRADA_INVALIDA},
        summary="Altera dados do contratante, quota ou status",
        tags=["credenciais"],
    )
    async def alterar(request: Request, chave: str, dados: AlteracaoCredencial):
        campos = {k: v for k, v in dados.model_dump().items() if v is not None}
        if not campos:
            raise HTTPException(400, "nenhum campo para alterar")
        _cred(request, chave)                       # 404 antes de tentar alterar
        try:
            alterada = request.app.state.credenciais.alterar(chave, **campos)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return alterada.para_json()

    @app.post(
        "/manager/credenciais/{chave}/rotacionar",
        response_model=esquemas.SegredoRotacionado,
        responses=esquemas.NAO_ENCONTRADA,
        summary="Gera um segredo novo, mantendo a chave",
        description="O cliente atualiza um valor, não dois, e o histórico de "
                    "uso continua ligado à mesma credencial.",
        tags=["credenciais"],
    )
    async def rotacionar(request: Request, chave: str):
        _cred(request, chave)
        secret = request.app.state.credenciais.rotacionar(chave)
        return {"consumerKey": chave, "consumerSecret": secret,
                "aviso": "o segredo anterior deixou de valer imediatamente"}

    @app.post(
        "/manager/credenciais/{chave}/token-mcp",
        response_model=esquemas.TokenMCP,
        responses={**esquemas.NAO_ENCONTRADA, **esquemas.ENTRADA_INVALIDA},
        summary="Emite um token de longa duração para o endpoint MCP",
        description="Para clientes de agente que só aceitam cabeçalho estático. "
                    "O token vale **só** no `/mcp` (claim `aud`), cai na hora "
                    "se a credencial for revogada ou suspensa, e cai também "
                    "se o segredo for rotacionado (claim `gen`). `dias` vai de "
                    "1 a 365.",
        tags=["credenciais"],
    )
    async def token_mcp(request: Request, chave: str, dias: int = 90):
        # Emitido AQUI, e não na API pública, pela mesma razão que o manager
        # existe separado: um token de meses é coisa que só o operador cunha.
        if not cfg.mcp_uri:
            raise HTTPException(
                400, "MCP desligado: API_MCP_URI não está configurada neste processo"
            )
        if not 1 <= dias <= 365:
            raise HTTPException(400, "dias deve estar entre 1 e 365")
        cred = _cred(request, chave)
        if not cred.ativa:
            raise HTTPException(400, f"credencial {cred.status}: não emito token para ela")

        agora = int(time.time())
        token, validade = mod_token.emitir(
            chave, cfg.jwt_segredo, validade_s=dias * 86400, escopo="mcp",
            agora=agora, audiencia=cfg.mcp_uri,
            geracao=request.app.state.credenciais.geracao(chave),
        )

        def _iso(t: int) -> str:
            return datetime.fromtimestamp(t, timezone.utc).isoformat()

        return {
            "consumerKey": chave,
            "token": token,
            "audiencia": cfg.mcp_uri,
            "emissao": _iso(agora),
            "expiraEm": _iso(agora + validade),
            "aviso": "guarde o token: ele não é armazenado. Rotacionar o segredo "
                     "ou revogar a credencial o invalida.",
        }

    @app.delete(
        "/manager/credenciais/{chave}",
        response_model=esquemas.Credencial,
        responses=esquemas.NAO_ENCONTRADA,
        summary="Revoga uma credencial",
        description="**Não apaga.** Marca `status` e `revogadaEm`, porque o "
                    "uso histórico precisa continuar íntegro nas estatísticas.",
        tags=["credenciais"],
    )
    async def revogar(request: Request, chave: str):
        _cred(request, chave)
        # Revogar NÃO apaga: o uso histórico precisa continuar íntegro nas
        # estatísticas. Credencial apagada deixaria buraco no relatório do mês.
        return request.app.state.credenciais.revogar(chave).para_json()

    # -- estatísticas ---------------------------------------------------

    @app.get(
        "/manager/estatisticas",
        response_model=esquemas.Estatisticas,
        summary="Uso geral, sintético",
        tags=["estatísticas"],
    )
    async def estatisticas_gerais(request: Request, competencia: str | None = None):
        request.app.state.estatisticas.consolidar(cfg.logs)
        return request.app.state.estatisticas.resumo(competencia=competencia)

    @app.get(
        "/manager/estatisticas/{chave}",
        response_model=esquemas.EstatisticasDaCredencial,
        responses=esquemas.NAO_ENCONTRADA,
        summary="Uso de uma credencial, com a quota contratada",
        tags=["estatísticas"],
    )
    async def estatisticas_da_credencial(request: Request, chave: str,
                                         competencia: str | None = None):
        cred = _cred(request, chave)
        request.app.state.estatisticas.consolidar(cfg.logs)
        resumo = request.app.state.estatisticas.resumo(chave, competencia)
        resumo["quotaMensal"] = cred.quota_mensal
        resumo["consumoDoMes"] = request.app.state.estatisticas.consumo_do_mes(chave)
        if cred.quota_mensal:
            resumo["percentualDaQuota"] = round(
                resumo["consumoDoMes"] / cred.quota_mensal * 100, 1
            )
        return resumo

    @app.post(
        "/manager/manutencao/consolidar",
        response_model=esquemas.Consolidacao,
        summary="Consolida o log analítico no sintético",
        description="Idempotente: rodar duas vezes não conta duas vezes.",
        tags=["manutenção"],
    )
    async def consolidar(request: Request, incluir_hoje: bool = False):
        return request.app.state.estatisticas.consolidar(cfg.logs, incluir_hoje)

    @app.post(
        "/manager/manutencao/podar",
        response_model=esquemas.Poda,
        responses=esquemas.ENTRADA_INVALIDA,
        summary="Apaga log analítico já consolidado",
        description="**Não há janela de retenção automática.** `dias` é "
                    "obrigatório: apagar histórico é sempre ato deliberado.",
        tags=["manutenção"],
    )
    async def podar(request: Request, dias: int | None = None):
        """Apaga o analítico já consolidado mais velho que `dias`.

        **Nada poda sozinho.** Não há janela de retenção automática: o log
        analítico é mantido indefinidamente, e apagá-lo é sempre um ato
        deliberado com o prazo dito na chamada. `dias` sem valor, ou
        `retencao_dias = 0`, recusa em vez de assumir um padrão -- apagar
        histórico por engano não tem desfazer.
        """
        prazo = dias if dias is not None else cfg.retencao_dias
        if not prazo:
            raise HTTPException(
                400, "informe 'dias': não há janela de retenção automática"
            )
        return {"arquivosApagados": request.app.state.estatisticas.podar(cfg.logs, prazo)}

    return app
