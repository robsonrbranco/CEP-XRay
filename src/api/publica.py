"""API pública — compatível com a Busca CEP dos Correios.

    POST /token/v1/autentica                        emite o Bearer
    GET  /cep/v2/enderecos/{cep}                    endereço por CEP
    GET  /cep/v2/enderecos                          listagem paginada
    GET  /cep/v1/localidades[/{uf}]                 municípios
    GET  /cep/v1/bairros/{uf}/localidades/{id}      bairros
    GET  /cep/v1/ufs[/{uf}]                         unidades federativas
    GET  /cep/v1/atualizacao                        competência servida
    GET  /cep/v1/localidades/cliques                501
    GET  /cep/v1/localidades/caixas-postais         501
    GET  /cep/v1/localidades/agencias-modulares     501

O prefixo é `/cep/` porque é o caminho que o cliente escreve
(`https://api.correios.com.br/cep/v2/enderecos/...`). `/token/v1/autentica`
fica fora dele, como nos Correios, e `/saude` fica fora dos dois.

Esta aplicação **não** tem a rota `/manager`: gestão de credenciais roda noutra
porta, não exposta. Uma credencial capaz de criar credenciais seria escalada de
privilégio, e separar por processo é a única barreira que não depende de nenhum
segredo estar certo.

Sobre as rotas 501
------------------
Três recursos do manual respondem "quais LOCALIDADES oferecem tal serviço", e
nenhuma das 15 tabelas do extrato tem atributo de serviço. Eles existem aqui,
documentados no OpenAPI, devolvendo 501 — um cliente gerado da especificação
tem o método, e recebe uma recusa honesta em vez de uma lista inventada.

A tentação foi medida e recusada: `log_complemento.complemento` contém o texto
`Clique e Retire Correios` em 8.481 linhas, `locker` em 119, `caixa postal` em
4 e `agência modular` em 0. Daria para derivar as listas por `LIKE` em prosa.
Não dá: esses textos descrevem *aquele CEP*, não a cobertura da localidade, e
para caixas-postais (4 acertos) seria um palpite obviamente errado. Os números
ficam aqui para a próxima pessoa saber que a ideia foi avaliada, não esquecida.

Sobre os códigos de erro
------------------------
Os códigos seguem o manual. O **corpo** da resposta de erro não foi conferido
contra uma resposta real dos Correios — o Swagger deles exige credencial CWS, e
o manual público lista os códigos, não o JSON. `MENSAGENS` centraliza o texto;
se um cliente real depender do formato do corpo, é ali que se ajusta.

Quota estourada devolve **403**, não 429, pela mesma razão do molde: é restrição
contratual, e 429 introduziria um código que um cliente escrito para o contrato
original não espera.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import cep as mod_cep
from . import correios, esquemas
from . import token as mod_token
from .conexao import ConexaoViva
from .config import ConfigAPI
from .credenciais import Credenciais
from .uso import Estatisticas, Log

MENSAGENS = {
    400: "O CEP informado não é válido",
    401: "Houve falha na autenticação",
    403: "Acesso negado",
    404: "Nenhum registro encontrado para o CEP informado",
    500: "Ocorreu um erro interno inesperado",
    501: "Recurso não disponível nesta implementação",
}

DESCRICAO = """API de consulta de CEP compatível com a Busca CEP dos Correios:
mesmos caminhos, mesmos nomes de campo, mesmos códigos de retorno.

**Três diferenças que nenhuma implementação fecha**, porque são da fonte e não
do código:

* a base é um **retrato de uma competência** do extrato DNEC, não o DNE ao vivo;
* **seis campos saem sempre nulos** — `abreviatura`, `numeroLocalidade`,
  `cepUnidadeOperacional`, `numeroInicial`, `numeroFinal` e, fora de grande
  usuário, `nomeUnidade`. O extrato não os traz, e preenchê-los exigiria
  inventar dado. Ver a descrição de cada um no schema;
* **três recursos devolvem 501** — cliques, caixas postais e agências modulares
  respondem sobre cobertura de serviço por localidade, e o extrato não tem
  atributo de serviço.
"""

AMBIENTE = "PRODUCAO"


class ErroAPI(Exception):
    def __init__(self, status: int, mensagem: str | None = None):
        self.status = status
        self.mensagem = mensagem or MENSAGENS.get(status, "Erro")
        super().__init__(self.mensagem)


class Consultas:
    """Envolve as funções de consulta com a conexão viva do worker.

    Existe para que `criar_app` receba UM objeto injetável: o teste passa um
    dublê com os mesmos métodos e roda sem Firebird.
    """

    def __init__(self, viva: ConexaoViva):
        self._viva = viva

    def cep(self, valor: str):
        from ..consulta.cep import consultar

        return self._viva.executar(lambda con: consultar(valor, con=con))

    def enderecos(self, **kw):
        from ..consulta import listagem

        return self._viva.executar(lambda con: listagem.enderecos(con, **kw))

    def ufs(self):
        from ..consulta import listagem

        return self._viva.executar(listagem.ufs)

    def uf(self, sigla: str):
        from ..consulta import listagem

        return self._viva.executar(lambda con: listagem.uf(con, sigla))

    def localidades(self, **kw):
        from ..consulta import listagem

        return self._viva.executar(lambda con: listagem.localidades(con, **kw))

    def bairros(self, **kw):
        from ..consulta import listagem

        return self._viva.executar(lambda con: listagem.bairros(con, **kw))

    def atualizacao(self):
        from ..consulta import listagem

        return self._viva.executar(listagem.atualizacao)


# Filtros que o manual descreve e que esta base não alimenta. Usar um deles
# devolve 501 nomeando o filtro — ignorá-lo e responder 200 com o conjunto não
# filtrado seria pior: o cliente acharia que filtrou.
FILTROS_NAO_ALIMENTAVEIS = (
    "siglaUnidade", "clique", "caixaPostal", "locker", "agenciaModular",
    "numeroCaixaPostal", "numero",
)


def criar_app(cfg: ConfigAPI, consultas=None) -> FastAPI:
    """Monta a aplicação.

    `consultas` é injetável para o teste rodar sem Firebird. O padrão é a
    consulta real; substituí-la não muda nenhum caminho de código testado.
    """
    # Conexão viva por worker. Abrir attachment em embedded custa ~390 ms e a
    # consulta indexada custa menos de 1 ms — sem isto a API pagaria o
    # attachment a cada requisição.
    viva: ConexaoViva | None = None
    if consultas is None:
        viva = ConexaoViva()
        consultas = Consultas(viva)

    # `auto_error=False` porque quem decide o código é a API: sem isto o
    # FastAPI devolveria 403 onde o contrato manda 401.
    basic = HTTPBasic(auto_error=False, description="Usuário e código de acesso")
    bearer = HTTPBearer(
        auto_error=False, description="Token obtido em POST /token/v1/autentica"
    )

    app = FastAPI(
        title="CEP-XRay — Busca CEP",
        version="2.0",
        description=DESCRICAO,
    )

    # Abertos na construção, não no lifespan: importar a aplicação sem executá-la
    # devolve um objeto funcionando. Cada worker constrói o seu, e é isso que
    # dispensa lock no log — nenhum arquivo é compartilhado entre processos.
    app.state.conexao = viva
    app.state.consultas = consultas
    app.state.credenciais = Credenciais(cfg.credenciais)
    app.state.log = Log(cfg.logs)
    app.state.estatisticas = Estatisticas(cfg.estatisticas)

    # O FastAPI injeta 422 em toda rota com parâmetro, e esta API nunca o
    # devolve: os parâmetros são strings e a validação é feita no handler,
    # virando 400. Documentá-lo mandaria o cliente tratar um caso inexistente.
    def _openapi_sem_422():
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(
            title=app.title, version=app.version,
            description=app.description, routes=app.routes,
        )
        for caminho in spec.get("paths", {}).values():
            for operacao in caminho.values():
                if isinstance(operacao, dict):
                    operacao.get("responses", {}).pop("422", None)
        esquemas_spec = spec.get("components", {}).get("schemas", {})
        esquemas_spec.pop("HTTPValidationError", None)
        esquemas_spec.pop("ValidationError", None)
        app.openapi_schema = spec
        return spec

    app.openapi = _openapi_sem_422

    # -- erros ----------------------------------------------------------

    @app.exception_handler(ErroAPI)
    async def _erro_api(request: Request, exc: ErroAPI):
        return JSONResponse(status_code=exc.status, content={"message": exc.mensagem})

    @app.exception_handler(Exception)
    async def _erro_interno(request: Request, exc: Exception):
        # Nunca vaza a exceção: mensagem de erro é superfície de informação.
        return JSONResponse(status_code=500, content={"message": MENSAGENS[500]})

    # -- saúde ----------------------------------------------------------

    @app.get(
        "/saude",
        response_model=esquemas.Saude,
        summary="Estado do pod e competência dos dados servidos",
        description="**Não faz parte do contrato dos Correios** — é nossa, e "
                    "fica fora de `/cep/` para não colidir com nenhum caminho "
                    "futuro deles.\n\nSem autenticação: serve de sonda de "
                    "vivacidade, e a competência é o oposto de informação "
                    "sensível — quem consome precisa dela para saber a idade "
                    "do dado.",
        tags=["operação"],
    )
    async def saude(request: Request):
        def ler(con):
            from ..db import manage

            meta = manage.ler_metadados(con)
            cur = con.cursor()
            cur.execute("SELECT MON$READ_ONLY FROM MON$DATABASE")
            return meta, bool(cur.fetchone()[0])

        try:
            meta, somente_leitura = (
                request.app.state.conexao.executar(ler)
                if request.app.state.conexao is not None
                else ({}, None)
            )
        except Exception:  # noqa: BLE001
            # Sonda de vivacidade não pode derrubar o pod por causa do banco;
            # ela existe para REPORTAR que ele está fora.
            return JSONResponse(
                status_code=503, content={"status": "base inacessível"}
            )

        linhas = meta.get("linhas_total")
        return {
            "status": "ok",
            "competencia": meta.get("competencia") or None,
            "construidaEm": meta.get("construida_em") or None,
            "linhas": int(linhas) if linhas and linhas.isdigit() else None,
            "baseSomenteLeitura": somente_leitura,
        }

    # -- token ----------------------------------------------------------

    @app.post(
        "/token/v1/autentica",
        status_code=201,
        response_model=esquemas.Token,
        responses=esquemas.ERROS_TOKEN,
        summary="Emite o token de acesso",
        description="Envie `Authorization: Basic base64(usuario:codigoAcesso)`. "
                    "Devolve **201**, como os Correios.",
        tags=["autenticação"],
    )
    async def autentica(request: Request, credencial=Depends(basic)):
        authorization = request.headers.get("authorization")
        try:
            chave, segredo = mod_token.credenciais_basic(authorization)
        except mod_token.TokenInvalido as e:
            raise ErroAPI(401) from e

        cred = request.app.state.credenciais.autenticar(chave, segredo)
        if cred is None:
            raise ErroAPI(401)

        agora = datetime.now(timezone.utc).replace(microsecond=0)
        token, validade = mod_token.emitir(
            cred.consumer_key, cfg.jwt_segredo, cfg.validade_token_s
        )
        return JSONResponse(
            status_code=201,
            content={
                "token": token,
                "emissao": agora.isoformat(),
                "expiraEm": (agora + timedelta(seconds=validade)).isoformat(),
                "ambiente": AMBIENTE,
                "apis": ["cep"],
            },
        )

    # -- autenticação ---------------------------------------------------

    async def autenticado(request: Request, credencial=Depends(bearer)) -> str:
        try:
            bruto = mod_token.do_cabecalho(request.headers.get("authorization"))
            claims = mod_token.verificar(bruto, cfg.jwt_segredo)
        except mod_token.TokenInvalido as e:
            raise ErroAPI(401) from e

        chave = claims.get("sub", "")
        try:
            cred = request.app.state.credenciais.obter(chave)
        except LookupError as e:
            raise ErroAPI(401) from e
        # É o que torna a revogação imediata apesar do token ser stateless.
        if not cred.ativa:
            raise ErroAPI(401)

        if cred.quota_mensal is not None:
            consumo = request.app.state.estatisticas.consumo_do_mes(chave)
            if consumo >= cred.quota_mensal:
                raise ErroAPI(403, "Quota mensal do contrato excedida")
        return chave

    # -- o caminho comum ------------------------------------------------

    def _responder(monta, chave: str, rota: str, request: Request) -> Response:
        """Executa, registra no log e traduz exceção em código.

        O registro acontece no `finally`, SEMPRE, inclusive em erro: é o que
        permite distinguir consulta faturável de erro de cliente.
        """
        inicio = time.perf_counter()
        status, corpo = 200, None
        try:
            corpo = monta()
        except mod_cep.CEPInvalido:
            status = 400
        except correios.CEPNaoEncontrado:
            status = 404
        except ValueError as e:
            status, corpo = 400, {"message": str(e)}
        except ErroAPI as e:
            status = e.status
            corpo = {"message": e.mensagem}
        except Exception:  # noqa: BLE001
            status = 500
        finally:
            # O CEP CONSULTADO NÃO É REGISTRADO, e isto é decisão, não omissão.
            #
            # `uso.Log.registrar` aceita um `ni=` e o molde (CNPJ-XRay) o
            # preenche com o CNPJ consultado. Aqui ele fica de fora: um CNPJ
            # identifica uma empresa, um CEP identifica onde alguém mora.
            # Guardar quais CEPs cada cliente consulta é montar, sem precisar,
            # um histórico de endereços pesquisados.
            #
            # O que se perde: não dá para investigar "por que este cliente
            # recebeu 404 ontem" a partir do log. O que se ganha é não ter esse
            # dado para vazar, e não precisar de política de retenção para ele.
            #
            # `rota` entra como PADRÃO (`/cep/v2/enderecos/{cep}`), não como
            # caminho concreto — senão o CEP voltaria por essa porta, e agrupar
            # por rota viraria uma chave por CEP consultado.
            request.app.state.log.registrar(
                consumer_key=chave,
                rota=rota,
                status_http=status,
                duracao_ms=int((time.perf_counter() - inicio) * 1000),
            )
        if status != 200:
            if not isinstance(corpo, dict) or "message" not in corpo:
                corpo = {"message": MENSAGENS.get(status, "Erro")}
            return JSONResponse(status_code=status, content=corpo)
        return JSONResponse(status_code=200, content=corpo)

    def _recusar_filtros(request: Request) -> None:
        usados = [f for f in FILTROS_NAO_ALIMENTAVEIS if f in request.query_params]
        if usados:
            raise ErroAPI(
                501,
                f"Filtro não alimentável por esta base: {', '.join(usados)}. "
                f"O extrato DNEC não traz o dado que o sustenta.",
            )

    # -- endereços ------------------------------------------------------

    @app.get(
        "/cep/v2/enderecos/{cep}",
        response_model=esquemas.Endereco,
        responses=esquemas.ERROS_CONSULTA,
        summary="Endereço por CEP",
        description="Aceita com ou sem pontuação. **CEP não tem dígito "
                    "verificador**: 400 é só para o que não tem forma de CEP; "
                    "um CEP bem formado que não existe na base devolve 404.\n\n"
                    "CEP sem logradouro próprio é resolvido pela faixa do "
                    "município e sai com `tipoCEP: 2`.",
        tags=["endereços"],
    )
    async def endereco(request: Request, cep: str, chave: str = Depends(autenticado)):
        def monta():
            valor = mod_cep.normalizar(cep)
            return correios.endereco(request.app.state.consultas.cep(valor), valor)

        return _responder(monta, chave, "/cep/v2/enderecos/{cep}", request)

    @app.get(
        "/cep/v2/enderecos",
        response_model=esquemas.PaginaEnderecos,
        responses={**esquemas.ERROS_LISTAGEM, **esquemas.NAO_ALIMENTAVEL},
        summary="Listagem paginada de endereços",
        description="Exige ao menos um filtro entre `uf`, `localidade`, "
                    "`bairro` e `logradouro` — sem filtro, a resposta seria "
                    "uma paginação sobre 1,72 milhão de linhas.\n\n"
                    "A comparação por nome ignora acento e caixa (collation "
                    "`WIN_PTBR`): `localidade=sao paulo` acha *São Paulo*. "
                    "`logradouro` casa por **prefixo**; busca por trecho no "
                    "meio do nome não existe no Firebird 3.0.",
        tags=["endereços"],
    )
    async def listar_enderecos(
        request: Request,
        uf: str | None = Query(default=None),
        localidade: int | None = Query(default=None, description="id da localidade"),
        bairro: str | None = Query(default=None),
        logradouro: str | None = Query(default=None),
        page: int = Query(default=0, ge=0),
        size: int = Query(default=50, ge=1),
        chave: str = Depends(autenticado),
    ):
        def monta():
            _recusar_filtros(request)
            if uf and not mod_cep.uf_valida(uf):
                raise ValueError(f"UF inválida: {uf}")
            fichas, total = request.app.state.consultas.enderecos(
                uf=uf, id_localidade=localidade, bairro=bairro,
                logradouro=logradouro, pagina=page, tamanho=size,
            )
            itens = [correios.endereco(f, f["cep"]) for f in fichas]
            return correios.pagina(itens, page, size, total)

        return _responder(monta, chave, "/cep/v2/enderecos", request)

    # -- os três 501 ----------------------------------------------------
    #
    # Registrados como rotas de verdade para que um cliente gerado do OpenAPI
    # tenha o método. Ver a nota no topo do módulo sobre a derivação por LIKE
    # que foi medida e recusada.
    #
    # E registrados ANTES de `/cep/v1/localidades/{uf}`, o que não é estilo:
    # o Starlette casa na ordem de registro, e `{uf}` casaria `cliques` como
    # se fosse uma sigla. O cliente receberia 400 "UF inválida" no lugar do
    # 501 que explica o que de fato falta. Travado por teste.

    def _rota_501(caminho: str, resumo: str, porque: str):
        @app.get(
            caminho,
            responses={**esquemas.NAO_ALIMENTAVEL, 401: esquemas.ERROS_TOKEN[401]},
            status_code=501,
            summary=resumo,
            description=porque,
            tags=["não implementado"],
        )
        async def _handler(request: Request, chave: str = Depends(autenticado)):
            return _responder(
                lambda: (_ for _ in ()).throw(ErroAPI(501, porque)),
                chave, caminho, request,
            )

        return _handler

    _rota_501(
        "/cep/v1/localidades/cliques",
        "Localidades com Clique e Retire (501)",
        "O extrato DNEC não tem atributo de serviço por localidade. O texto "
        "'Clique e Retire Correios' aparece em 8.481 complementos, mas descreve "
        "aquele CEP, não a cobertura do município — derivar a lista dali seria "
        "um palpite com aparência de resposta.",
    )
    _rota_501(
        "/cep/v1/localidades/caixas-postais",
        "Localidades com caixa postal (501)",
        "O extrato DNEC não tem atributo de serviço por localidade. 'caixa "
        "postal' aparece em 4 complementos de 241.725 — número que por si só "
        "mostra que não é a cobertura do serviço.",
    )
    _rota_501(
        "/cep/v1/localidades/agencias-modulares",
        "Localidades com agência modular (501)",
        "O extrato DNEC não tem atributo de serviço por localidade, e 'agência "
        "modular' não aparece em nenhum complemento.",
    )

    # -- localidades ----------------------------------------------------

    @app.get(
        "/cep/v1/localidades",
        response_model=esquemas.PaginaLocalidades,
        responses={**esquemas.ERROS_LISTAGEM, **esquemas.NAO_ALIMENTAVEL},
        summary="Municípios",
        tags=["localidades"],
    )
    async def listar_localidades(
        request: Request,
        uf: str | None = Query(default=None),
        nome: str | None = Query(default=None, description="casa por prefixo"),
        page: int = Query(default=0, ge=0),
        size: int = Query(default=50, ge=1),
        chave: str = Depends(autenticado),
    ):
        return _responder(
            lambda: _localidades(request, uf, nome, page, size),
            chave, "/cep/v1/localidades", request,
        )

    @app.get(
        "/cep/v1/localidades/{uf}",
        response_model=esquemas.PaginaLocalidades,
        responses={**esquemas.ERROS_LISTAGEM, **esquemas.NAO_ALIMENTAVEL},
        summary="Municípios de uma UF",
        tags=["localidades"],
    )
    async def localidades_da_uf(
        request: Request,
        uf: str,
        nome: str | None = Query(default=None),
        page: int = Query(default=0, ge=0),
        size: int = Query(default=50, ge=1),
        chave: str = Depends(autenticado),
    ):
        return _responder(
            lambda: _localidades(request, uf, nome, page, size),
            chave, "/cep/v1/localidades/{uf}", request,
        )

    def _localidades(request, uf, nome, page, size):
        _recusar_filtros(request)
        if uf and not mod_cep.uf_valida(uf):
            raise ValueError(f"UF inválida: {uf}")
        linhas, total = request.app.state.consultas.localidades(
            uf=uf, nome=nome, pagina=page, tamanho=size
        )
        if uf and total == 0:
            raise correios.CEPNaoEncontrado(uf)
        return correios.pagina(
            [correios.localidade(l) for l in linhas], page, size, total
        )

    # -- bairros --------------------------------------------------------

    @app.get(
        "/cep/v1/bairros/{uf}/localidades/{localidade}",
        response_model=esquemas.PaginaBairros,
        responses=esquemas.ERROS_LISTAGEM,
        summary="Bairros de um município",
        tags=["localidades"],
    )
    async def listar_bairros(
        request: Request,
        uf: str,
        localidade: int,
        page: int = Query(default=0, ge=0),
        size: int = Query(default=50, ge=1),
        chave: str = Depends(autenticado),
    ):
        def monta():
            if not mod_cep.uf_valida(uf):
                raise ValueError(f"UF inválida: {uf}")
            linhas, total = request.app.state.consultas.bairros(
                uf=uf, id_cidade=localidade, pagina=page, tamanho=size
            )
            if total == 0:
                raise correios.CEPNaoEncontrado(f"{uf}/{localidade}")
            return correios.pagina(
                [correios.bairro(b) for b in linhas], page, size, total
            )

        return _responder(
            monta, chave, "/cep/v1/bairros/{uf}/localidades/{localidade}", request
        )

    # -- UFs ------------------------------------------------------------

    @app.get(
        "/cep/v1/ufs",
        response_model=list[esquemas.UF],
        responses=esquemas.ERROS_LISTAGEM,
        summary="Unidades federativas e suas faixas de CEP",
        tags=["localidades"],
    )
    async def listar_ufs(request: Request, chave: str = Depends(autenticado)):
        return _responder(
            lambda: [correios.uf(u) for u in request.app.state.consultas.ufs()],
            chave, "/cep/v1/ufs", request,
        )

    @app.get(
        "/cep/v1/ufs/{uf}",
        response_model=esquemas.UF,
        responses=esquemas.ERROS_LISTAGEM,
        summary="Uma unidade federativa",
        tags=["localidades"],
    )
    async def uma_uf(request: Request, uf: str, chave: str = Depends(autenticado)):
        def monta():
            if not mod_cep.uf_valida(uf):
                raise ValueError(f"UF inválida: {uf}")
            linha = request.app.state.consultas.uf(uf)
            if linha is None:
                raise correios.CEPNaoEncontrado(uf)
            return correios.uf(linha)

        return _responder(monta, chave, "/cep/v1/ufs/{uf}", request)

    # -- atualização ----------------------------------------------------

    @app.get(
        "/cep/v1/atualizacao",
        response_model=esquemas.Atualizacao,
        responses=esquemas.ERROS_LISTAGEM,
        summary="Competência do extrato que este pod está servindo",
        tags=["operação"],
    )
    async def atualizacao(request: Request, chave: str = Depends(autenticado)):
        return _responder(
            request.app.state.consultas.atualizacao,
            chave, "/cep/v1/atualizacao", request,
        )

    # -- site -----------------------------------------------------------
    #
    # Montado por ÚLTIMO, e não é estilo: o Starlette casa rotas na ordem de
    # registro e `Mount("/")` casa tudo. Montado antes, engoliria `/saude`,
    # `/cep/...`, `/token/...` e `/docs`.
    if cfg.site_dir and Path(cfg.site_dir).is_dir():
        app.mount(
            "/", StaticFiles(directory=cfg.site_dir, html=True), name="site"
        )

    return app
