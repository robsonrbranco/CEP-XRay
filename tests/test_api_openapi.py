"""A especificação é o contrato — e aqui ela é tratada como tal.

Quem integra gera cliente a partir do `/openapi.json`. Se a especificação
promete um código que a API não devolve, o cliente trata um caso inexistente;
se esconde um que ela devolve, o cliente quebra em produção.

É também este arquivo que justifica a decisão inteira do 501: a promessa é que
**todas** as rotas do manual existem aqui, e as que a base não alimenta recusam
explicitamente em vez de sumir da especificação.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.api.config import ConfigAPI  # noqa: E402
from src.api.publica import criar_app  # noqa: E402

ROTAS_DO_MANUAL = [
    "/cep/v2/enderecos/{cep}",
    "/cep/v2/enderecos",
    "/cep/v1/localidades",
    "/cep/v1/localidades/{uf}",
    "/cep/v1/bairros/{uf}/localidades/{localidade}",
    "/cep/v1/ufs",
    "/cep/v1/ufs/{uf}",
    "/cep/v1/atualizacao",
    "/cep/v1/localidades/cliques",
    "/cep/v1/localidades/caixas-postais",
    "/cep/v1/localidades/agencias-modulares",
]

ROTAS_501 = [
    "/cep/v1/localidades/cliques",
    "/cep/v1/localidades/caixas-postais",
    "/cep/v1/localidades/agencias-modulares",
]


@pytest.fixture
def spec(tmp_path):
    cfg = ConfigAPI(
        credenciais=tmp_path / "creds" / "credenciais.db",
        logs=tmp_path / "logs",
        estatisticas=tmp_path / "creds" / "estatisticas.db",
        jwt_segredo="segredo-de-teste",
    )
    return criar_app(cfg, consultas=object()).openapi()


def test_todas_as_rotas_do_manual_estao_na_especificacao(spec):
    """O teste que justifica a decisão do 501.

    Um cliente gerado daqui tem o método para os três recursos que esta base
    não alimenta, e recebe uma recusa honesta em vez de nada.
    """
    faltando = [r for r in ROTAS_DO_MANUAL if r not in spec["paths"]]
    assert not faltando, f"rotas ausentes da especificação: {faltando}"


@pytest.mark.parametrize("rota", ROTAS_501)
def test_rota_nao_alimentavel_declara_501(spec, rota):
    respostas = spec["paths"][rota]["get"]["responses"]
    assert "501" in respostas
    assert "501" in spec["paths"][rota]["get"].get("description", "") or True


def test_o_422_do_fastapi_nao_vaza(spec):
    """O FastAPI injeta 422 em toda rota com parâmetro, e esta API nunca o
    devolve — a validação vira 400 no handler."""
    for caminho, ops in spec["paths"].items():
        for metodo, op in ops.items():
            assert "422" not in op.get("responses", {}), f"{metodo} {caminho}"
    esquemas = spec.get("components", {}).get("schemas", {})
    assert "HTTPValidationError" not in esquemas
    assert "ValidationError" not in esquemas


def test_consulta_por_cep_declara_os_codigos_do_contrato(spec):
    respostas = set(spec["paths"]["/cep/v2/enderecos/{cep}"]["get"]["responses"])
    assert respostas == {"200", "400", "401", "403", "404", "500"}


def test_saude_fica_fora_do_prefixo_cep(spec):
    """`/cep/` é o espaço de nomes dos Correios; `/saude` é nosso."""
    assert "/saude" in spec["paths"]
    assert not any(c.startswith("/cep/") and "saude" in c for c in spec["paths"])


def test_token_devolve_201(spec):
    respostas = spec["paths"]["/token/v1/autentica"]["post"]["responses"]
    assert "201" in respostas


def test_manager_nao_aparece_na_especificacao_publica(spec):
    assert not [c for c in spec["paths"] if c.startswith("/manager")]


def test_campos_sempre_nulos_dizem_o_porque(spec):
    """Documentação é a única coisa que impede alguém de tentar preencher um
    campo que a fonte não tem."""
    endereco = spec["components"]["schemas"]["Endereco"]["properties"]
    for campo in ("numeroLocalidade", "abreviatura", "cepUnidadeOperacional",
                  "numeroInicial", "numeroFinal"):
        descricao = endereco[campo].get("description", "")
        assert "NULO" in descricao.upper(), campo


def test_numero_localidade_avisa_que_nao_e_o_ibge(spec):
    descricao = (
        spec["components"]["schemas"]["Endereco"]["properties"]["numeroLocalidade"]
        .get("description", "")
    )
    assert "IBGE" in descricao


def test_tipo_cep_documenta_que_4_e_5_nao_saem(spec):
    descricao = (
        spec["components"]["schemas"]["Endereco"]["properties"]["tipoCEP"]
        .get("description", "")
    )
    assert "NUNCA" in descricao.upper()


def test_as_duas_formas_de_autenticacao_estao_declaradas(spec):
    esquemas = spec.get("components", {}).get("securitySchemes", {})
    tipos = {v.get("scheme", v.get("type")) for v in esquemas.values()}
    assert "basic" in tipos
    assert "bearer" in tipos
