"""Tradução da ficha interna para o contrato da Busca CEP dos Correios.

Camada pura: não toca no banco nem em HTTP. Recebe o dicionário que
`src.consulta.cep.consultar` devolve e emite a forma que os Correios
documentam — mesmos nomes de campo, mesmo camelCase, que não é a convenção do
resto deste projeto.

O que esta base NÃO tem, e sai nulo
-----------------------------------
Os campos abaixo **existem na resposta e valem `null`**, nunca ausentes — um
cliente gerado a partir do OpenAPI espera a chave, e omiti-la quebraria a
desserialização dele. Cada um leva a razão no `Field(description=...)` de
`esquemas.py`:

    abreviatura            é `LOG_NO_ABREV` do DNE; o extrato não a traz
    numeroLocalidade       é o código de localidade dos Correios (`LOC_NU`)
    cepUnidadeOperacional  não existe no extrato
    numeroInicial          ver abaixo
    numeroFinal            ver abaixo

**`numeroLocalidade` nunca recebe o código do IBGE.** Temos `cidade.id_cidade`
(chave surrogate do extrato) e `cidade.cidade_ibge` (numeração do IBGE, outra
coisa inteiramente). Devolver a numeração do IBGE num campo que promete a dos
Correios seria o pior erro possível aqui: o cliente não teria como perceber, e
usaria um número que casa com nada. O código do IBGE continua disponível, sob o
seu próprio nome, no recorte de localidade.

**`numeroInicial` e `numeroFinal` são parcialmente inferíveis e não são
inferidos.** O complemento traz prosa como `'até 318 lado par'` ou
`'de 320 ao fim lado par'`, e um regex tiraria números dali. Transformar prosa
em campo estruturado é inventar dado: `'ao fim'` não é um número, e o cliente
receberia um intervalo que a fonte nunca afirmou. O texto cru continua em
`complemento`, que é exatamente o que a fonte deu.

O que É derivado, e com que confiança
-------------------------------------
`lado` sai de uma busca de texto no complemento. Medido nos 241.725
complementos: 22.440 dizem `lado par`, 22.434 dizem `lado ímpar`, e 51 contêm
os dois (faixas compostas). Os 51 ambíguos saem **nulos** — é o caso em que o
campo não tem resposta única, e escolher uma seria chutar.

`tipoCEP` é derivado da procedência do registro, não de uma coluna. A tabela
abaixo é **nossa inferência sobre a semântica do DNE**, não um mapeamento que
os Correios publiquem campo a campo:

    1  logradouro        o CEP está em `logradouro` com tipo preenchido
    2  localidade        o CEP caiu numa FAIXA de município, sem logradouro
                         próprio — é o CEP geral da cidade
    3  grande usuário    o CEP está em `log_grande_usuario`

Os valores 4 (unidade operacional) e 5 (caixa postal comunitária) **nunca são
emitidos**: não há no extrato nada que os distinga dos demais. Emitir um deles
por palpite seria pior que não emitir.
"""

from __future__ import annotations

import re

# 'lado par' / 'lado ímpar' no texto do complemento. A collation do banco é
# insensível a acento, mas esta comparação é feita em Python, sobre o texto já
# lido — daí as duas grafias.
_PAR = re.compile(r"lado\s+par\b", re.I)
_IMPAR = re.compile(r"lado\s+[íi]mpar\b", re.I)

TIPO_LOGRADOURO = 1
TIPO_LOCALIDADE = 2
TIPO_GRANDE_USUARIO = 3


class CEPNaoEncontrado(LookupError):
    """CEP bem formado que não existe na base. Vira 404."""


def lado(complemento: str | None) -> str | None:
    """'P', 'I' ou None. Ambíguo é None — ver a nota no topo do módulo."""
    if not complemento:
        return None
    par, impar = bool(_PAR.search(complemento)), bool(_IMPAR.search(complemento))
    if par and impar:
        return None
    if par:
        return "P"
    if impar:
        return "I"
    return None


def tipo_cep(ficha: dict) -> int:
    if ficha.get("grande_usuario"):
        return TIPO_GRANDE_USUARIO
    if ficha.get("origem") == "faixa" or not ficha.get("tipo"):
        return TIPO_LOCALIDADE
    return TIPO_LOGRADOURO


def endereco(ficha: dict | None, cep: str) -> dict:
    """O recorte de `GET /cep/v2/enderecos/{cep}`."""
    if ficha is None:
        raise CEPNaoEncontrado(cep)

    cidade = ficha.get("cidade") or {}
    bairro = ficha.get("bairro") or {}
    complemento = ficha.get("complemento")

    return {
        "cep": ficha["cep"],
        "uf": ficha.get("estado"),
        "localidade": cidade.get("nome"),
        "numeroLocalidade": None,
        "logradouro": ficha.get("nome_logradouro"),
        "tipoLogradouro": ficha.get("tipo"),
        "complemento": complemento,
        "abreviatura": None,
        "bairro": bairro.get("nome") or None,
        "tipoCEP": tipo_cep(ficha),
        "cepUnidadeOperacional": None,
        "lado": lado(complemento),
        "numeroInicial": None,
        "numeroFinal": None,
        "nomeUnidade": ficha.get("grande_usuario"),
    }


def uf(linha: dict) -> dict:
    """O recorte de `GET /cep/v1/ufs/{uf}`.

    `faixas` é extensão nossa, não campo do contrato: o manual descreve a UF
    com uma faixa só, e a base tem até 3 por UF (Goiás, por exemplo, tem a
    faixa da capital destacada). Devolver uma e esconder as outras daria uma
    resposta que parece completa e não é.
    """
    return {
        "uf": linha["sigla"],
        "nome": linha.get("estado"),
        "capital": linha.get("capital"),
        "regiao": linha.get("regiao"),
        "faixaInicial": linha.get("faixa_ini"),
        "faixaFinal": linha.get("faixa_fim"),
        "latitude": linha.get("latitude"),
        "longitude": linha.get("longitude"),
        "faixas": linha.get("faixas") or [],
    }


def localidade(linha: dict) -> dict:
    """O recorte de `GET /cep/v1/localidades`.

    `numeroLocalidade` nulo pela mesma razão de sempre; `codigoIbge` sai sob o
    seu próprio nome, para não haver confusão entre as duas numerações.
    """
    return {
        "id": linha["id_cidade"],
        "nome": linha.get("cidade"),
        "uf": linha.get("estado"),
        "numeroLocalidade": None,
        "codigoIbge": linha.get("cidade_ibge"),
        "ddd": linha.get("ddd"),
        "latitude": linha.get("latitude"),
        "longitude": linha.get("longitude"),
        "faixaInicial": linha.get("faixa_ini"),
        "faixaFinal": linha.get("faixa_fim"),
    }


def bairro(linha: dict) -> dict:
    """O recorte de `GET /cep/v1/bairros/{uf}/localidades/{localidade}`."""
    return {
        "id": linha["id_bairro"],
        "nome": linha.get("bairro"),
        "uf": linha.get("estado"),
        "idLocalidade": linha.get("cidade_id"),
        "latitude": linha.get("latitude"),
        "longitude": linha.get("longitude"),
    }


def pagina(itens: list[dict], numero: int, tamanho: int, total: int) -> dict:
    """Envelope de paginação, no formato `itens` + `page` do manual."""
    total_paginas = (total + tamanho - 1) // tamanho if tamanho else 0
    return {
        "itens": itens,
        "page": {
            "number": numero,
            "size": tamanho,
            "totalElements": total,
            "totalPages": total_paginas,
        },
    }
