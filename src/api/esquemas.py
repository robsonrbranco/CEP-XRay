"""Modelos do contrato, para o OpenAPI descrever a API de verdade.

Sem isto o FastAPI gera `/docs` com as rotas listadas e **nenhum contrato**: o
schema do 200 sai como `{}`, os códigos 400/401/403/404/501 não aparecem, e
surge um 422 que a API real nunca devolve.

Estes modelos existem só para DOCUMENTAR. As rotas devolvem `JSONResponse`
direto e o FastAPI não valida nem filtra nada por causa deles — declarar em
`responses={...}` documenta sem interferir na resposta.

A escolha é deliberada: um `response_model` que valida silenciosamente
descartaria campo ausente do modelo, e a promessa aqui é compatibilidade com um
contrato de terceiros. Errar por documentação desatualizada é barato; errar
mutilando a resposta não é.

Os nomes seguem o contrato dos Correios, inclusive o camelCase, que não é a
convenção do resto do projeto.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Erro(BaseModel):
    message: str = Field(examples=["O CEP informado não é válido"])


class NaoImplementado(BaseModel):
    """Corpo do 501.

    Nomeia o que falta e por quê, em vez de devolver 501 mudo: quem integra
    precisa saber se é uma rota que nunca vai existir aqui ou um filtro que a
    base não alimenta.
    """

    message: str = Field(
        examples=["A base não tem o atributo de serviço por localidade"]
    )
    recurso: str | None = Field(
        default=None, examples=["/cep/v1/localidades/cliques"]
    )


class Saude(BaseModel):
    """Estado do pod e **proveniência do dado que ele está servindo**.

    `competencia` é o campo que importa. Sem ele, quem recebe uma resposta não
    tem como saber se o dado é de outubro ou de março — e a diferença aparece
    justamente onde dói: um CEP novo que ainda não entrou, uma rua que mudou
    de nome.
    """

    status: str = Field(examples=["ok"])
    competencia: str | None = Field(
        default=None, examples=["2026-10"],
        description="Competência do extrato DNEC de onde vieram os dados.",
    )
    construidaEm: str | None = Field(
        default=None, examples=["2026-09-21T15:53:47+00:00"],
        description="Quando a base foi construída — distinto da competência: "
                    "reconstruir uma competência antiga produz arquivo novo "
                    "com dado velho.",
    )
    linhas: int | None = None
    baseSomenteLeitura: bool | None = None


class Token(BaseModel):
    token: str = Field(examples=["eyJhbGciOiJIUzI1NiJ9..."])
    expiraEm: str = Field(examples=["2026-10-01T12:00:00+00:00"])
    emissao: str = Field(examples=["2026-10-01T11:00:00+00:00"])
    ambiente: str = Field(examples=["PRODUCAO"])
    apis: list[str] = Field(examples=[["cep"]])


class Endereco(BaseModel):
    cep: str = Field(examples=["01001000"])
    uf: str | None = Field(default=None, examples=["SP"])
    localidade: str | None = Field(default=None, examples=["São Paulo"])
    numeroLocalidade: int | None = Field(
        default=None,
        description="SEMPRE NULO. É o código de localidade dos Correios "
                    "(LOC_NU do DNE), que este extrato não traz. NÃO é o "
                    "código do IBGE — esse sai em `codigoIbge`, no recurso de "
                    "localidade, sob o seu próprio nome.",
    )
    logradouro: str | None = Field(default=None, examples=["da Sé"])
    tipoLogradouro: str | None = Field(default=None, examples=["Praça"])
    complemento: str | None = Field(default=None, examples=["lado ímpar"])
    abreviatura: str | None = Field(
        default=None,
        description="SEMPRE NULO. É LOG_NO_ABREV do DNE; o extrato não a traz.",
    )
    bairro: str | None = Field(default=None, examples=["Sé"])
    tipoCEP: int | None = Field(
        default=None, examples=[1],
        description="1 logradouro, 2 localidade (CEP geral de município, sem "
                    "logradouro próprio), 3 grande usuário. Os valores 4 "
                    "(unidade operacional) e 5 (caixa postal comunitária) "
                    "NUNCA são emitidos: nada no extrato os distingue.",
    )
    cepUnidadeOperacional: str | None = Field(
        default=None, description="SEMPRE NULO. Não existe no extrato."
    )
    lado: str | None = Field(
        default=None, examples=["I"],
        description="'P' ou 'I', derivado do texto do complemento. Nulo quando "
                    "o complemento não diz, ou quando diz os dois (51 casos "
                    "medidos de faixa composta).",
    )
    numeroInicial: str | None = Field(
        default=None,
        description="SEMPRE NULO. Seria parcialmente inferível por regex sobre "
                    "a prosa do complemento ('até 318 lado par'), e não é: "
                    "'ao fim' não é número, e o cliente receberia um intervalo "
                    "que a fonte nunca afirmou. O texto cru fica em "
                    "`complemento`.",
    )
    numeroFinal: str | None = Field(default=None, description="SEMPRE NULO.")
    nomeUnidade: str | None = Field(
        default=None, examples=["Empresa Brasileira de Correios e Telégrafos (ECT)"],
        description="Nome do grande usuário dono do CEP. Preenchido em 20.508 "
                    "dos CEPs da base; nulo nos demais.",
    )


class Faixa(BaseModel):
    faixaInicial: str | None = Field(default=None, examples=["01000000"])
    faixaFinal: str | None = Field(default=None, examples=["19999999"])


class UF(BaseModel):
    uf: str = Field(examples=["SP"])
    nome: str | None = Field(default=None, examples=["São Paulo"])
    capital: str | None = Field(default=None, examples=["São Paulo"])
    regiao: str | None = Field(default=None, examples=["Sudeste"])
    faixaInicial: str | None = None
    faixaFinal: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    faixas: list[Faixa] = Field(
        default_factory=list,
        description="Extensão nossa, não campo do manual: a base tem até 3 "
                    "faixas por UF (Goiás tem a da capital destacada), e "
                    "devolver só uma daria resposta que parece completa e não é.",
    )


class Localidade(BaseModel):
    id: int
    nome: str | None = None
    uf: str | None = None
    numeroLocalidade: int | None = Field(
        default=None, description="SEMPRE NULO. Ver `Endereco.numeroLocalidade`."
    )
    codigoIbge: str | None = Field(default=None, examples=["3550308"])
    ddd: str | None = Field(default=None, examples=["11"])
    latitude: float | None = None
    longitude: float | None = None
    faixaInicial: str | None = None
    faixaFinal: str | None = None


class Bairro(BaseModel):
    id: int
    nome: str | None = None
    uf: str | None = None
    idLocalidade: int | None = None
    latitude: float | None = None
    longitude: float | None = None


class Pagina(BaseModel):
    number: int
    size: int
    totalElements: int
    totalPages: int


class PaginaEnderecos(BaseModel):
    itens: list[Endereco]
    page: Pagina


class PaginaLocalidades(BaseModel):
    itens: list[Localidade]
    page: Pagina


class PaginaBairros(BaseModel):
    itens: list[Bairro]
    page: Pagina


class Atualizacao(BaseModel):
    competencia: str | None = Field(default=None, examples=["2026-10"])
    atualizadoEm: str | None = Field(
        default=None, examples=["2026-09-21T15:53:47+00:00"]
    )
    totalRegistros: int | None = Field(default=None, examples=[2171508])


def _erro(descricao: str) -> dict:
    return {"model": Erro, "description": descricao}


ERROS_CONSULTA = {
    400: _erro("O CEP informado não é válido"),
    401: _erro("Houve falha na autenticação"),
    403: _erro("Acesso negado"),
    404: _erro("Nenhum registro encontrado para o CEP informado"),
    500: _erro("Erro interno inesperado"),
}

ERROS_LISTAGEM = {
    400: _erro("Parâmetro inválido"),
    401: _erro("Houve falha na autenticação"),
    404: _erro("Nenhum registro encontrado"),
    500: _erro("Erro interno inesperado"),
}

ERROS_TOKEN = {401: _erro("Houve falha na autenticação")}

NAO_ALIMENTAVEL = {
    501: {
        "model": NaoImplementado,
        "description": "A base construída a partir do extrato DNEC não tem o "
                       "dado que alimenta este recurso. A rota existe no "
                       "contrato e devolve 501 em vez de um resultado "
                       "inventado.",
    }
}
