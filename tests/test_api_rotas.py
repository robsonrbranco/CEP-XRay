"""As rotas, de ponta a ponta, sem Firebird e sem rede.

A consulta é injetada em `criar_app`, o que faz toda a suite rodar em menos de
um segundo e no CI sem infraestrutura. O que se defende aqui é o contrato: qual
código sai em cada situação, e o que vai parar no log.
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.config import ConfigAPI  # noqa: E402
from src.api.credenciais import Credenciais  # noqa: E402
from src.api.publica import criar_app  # noqa: E402

CEP = "01001000"
CEP_INEXISTENTE = "00000000"


def _ficha(cep=CEP):
    return {
        "cep": cep, "origem": "logradouro", "tipo": "Praça",
        "nome_logradouro": "da Sé", "estado": "SP",
        "bairro": {"id": 1, "nome": "Sé"},
        "cidade": {"id": 1, "nome": "São Paulo", "ibge": "3550308", "ddd": "11"},
        "complemento": "lado ímpar", "grande_usuario": None,
    }


class ConsultasFalsas:
    """Mesmos métodos que `publica.Consultas`, sem banco."""

    def cep(self, valor):
        return _ficha(valor) if valor != CEP_INEXISTENTE else None

    def enderecos(self, **kw):
        if not any((kw.get("uf"), kw.get("id_localidade"),
                    kw.get("bairro"), kw.get("logradouro"))):
            raise ValueError("informe ao menos um filtro")
        return [_ficha()], 1

    def ufs(self):
        return [{"sigla": "SP", "estado": "São Paulo", "capital": "São Paulo",
                 "regiao": "Sudeste", "faixa_ini": "01000000",
                 "faixa_fim": "19999999", "faixas": []}]

    def uf(self, sigla):
        return self.ufs()[0] if sigla.upper() == "SP" else None

    def localidades(self, **kw):
        if kw.get("uf") and kw["uf"].upper() != "SP":
            return [], 0
        return [{"id_cidade": 1, "cidade": "São Paulo", "estado": "SP",
                 "cidade_ibge": "3550308", "ddd": "11"}], 1

    def bairros(self, **kw):
        if kw.get("id_cidade") != 1:
            return [], 0
        return [{"id_bairro": 1, "bairro": "Sé", "estado": "SP",
                 "cidade_id": 1}], 1

    def atualizacao(self):
        return {"competencia": "2026-10",
                "atualizadoEm": "2026-09-21T15:53:47+00:00",
                "totalRegistros": 2171508}


@pytest.fixture
def ambiente(tmp_path):
    cfg = ConfigAPI(
        credenciais=tmp_path / "creds" / "credenciais.db",
        logs=tmp_path / "logs",
        estatisticas=tmp_path / "creds" / "estatisticas.db",
        jwt_segredo="segredo-de-teste",
    )
    cred, segredo = Credenciais(cfg.credenciais).criar(contratante_nome="Teste")
    app = criar_app(cfg, consultas=ConsultasFalsas())
    cliente = TestClient(app, raise_server_exceptions=False)
    return cfg, cliente, cred, segredo


def _basic(cred, segredo):
    b = base64.b64encode(f"{cred.consumer_key}:{segredo}".encode()).decode()
    return {"Authorization": f"Basic {b}"}


def _bearer(cliente, cred, segredo):
    r = cliente.post("/token/v1/autentica", headers=_basic(cred, segredo))
    assert r.status_code == 201
    return {"Authorization": "Bearer " + r.json()["token"]}


def _linhas_do_log(cfg):
    linhas = []
    for f in sorted(cfg.logs.glob("consultas-*.jsonl")):
        linhas += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    return linhas


# -- token ------------------------------------------------------------------

def test_token_devolve_201_no_formato_dos_correios(ambiente):
    _, cliente, cred, segredo = ambiente
    r = cliente.post("/token/v1/autentica", headers=_basic(cred, segredo))
    assert r.status_code == 201
    assert set(r.json()) == {"token", "emissao", "expiraEm", "ambiente", "apis"}
    assert r.json()["apis"] == ["cep"]


@pytest.mark.parametrize("cabecalho", [{}, {"Authorization": "Basic lixo"},
                                       {"Authorization": "Bearer x"}])
def test_token_recusa_credencial_errada(ambiente, cabecalho):
    _, cliente, _, _ = ambiente
    assert cliente.post("/token/v1/autentica", headers=cabecalho).status_code == 401


def test_segredo_errado_da_401(ambiente):
    _, cliente, cred, _ = ambiente
    r = cliente.post("/token/v1/autentica", headers=_basic(cred, "errado"))
    assert r.status_code == 401


# -- endereços --------------------------------------------------------------

def test_endereco_por_cep(ambiente):
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    r = cliente.get(f"/cep/v2/enderecos/{CEP}", headers=tok)
    assert r.status_code == 200
    assert r.json()["localidade"] == "São Paulo"
    assert r.json()["tipoLogradouro"] == "Praça"


def test_cep_com_pontuacao_e_aceito(ambiente):
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    assert cliente.get("/cep/v2/enderecos/01001-000", headers=tok).status_code == 200


def test_forma_invalida_da_400_e_cep_inexistente_da_404(ambiente):
    """A distinção que justifica `api/cep.py` existir."""
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    assert cliente.get("/cep/v2/enderecos/abc", headers=tok).status_code == 400
    assert cliente.get("/cep/v2/enderecos/123", headers=tok).status_code == 400
    r = cliente.get(f"/cep/v2/enderecos/{CEP_INEXISTENTE}", headers=tok)
    assert r.status_code == 404


def test_consulta_sem_token_da_401(ambiente):
    _, cliente, _, _ = ambiente
    assert cliente.get(f"/cep/v2/enderecos/{CEP}").status_code == 401


def test_token_forjado_da_401(ambiente):
    _, cliente, _, _ = ambiente
    r = cliente.get(f"/cep/v2/enderecos/{CEP}",
                    headers={"Authorization": "Bearer a.b.c"})
    assert r.status_code == 401


def test_credencial_revogada_para_de_valer_imediatamente(ambiente):
    """O token continua criptograficamente válido; a credencial é que morreu."""
    cfg, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    assert cliente.get(f"/cep/v2/enderecos/{CEP}", headers=tok).status_code == 200
    Credenciais(cfg.credenciais).revogar(cred.consumer_key)
    assert cliente.get(f"/cep/v2/enderecos/{CEP}", headers=tok).status_code == 401


def test_quota_estourada_da_403(tmp_path):
    cfg = ConfigAPI(
        credenciais=tmp_path / "creds" / "credenciais.db",
        logs=tmp_path / "logs",
        estatisticas=tmp_path / "creds" / "estatisticas.db",
        jwt_segredo="segredo-de-teste",
    )
    cred, segredo = Credenciais(cfg.credenciais).criar(
        contratante_nome="Teste", quota_mensal=0
    )
    cliente = TestClient(criar_app(cfg, consultas=ConsultasFalsas()),
                         raise_server_exceptions=False)
    tok = _bearer(cliente, cred, segredo)
    assert cliente.get(f"/cep/v2/enderecos/{CEP}", headers=tok).status_code == 403


def test_listagem_sem_filtro_da_400(ambiente):
    """Sem filtro a resposta seria uma paginação sobre 1,72 milhão de linhas."""
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    assert cliente.get("/cep/v2/enderecos", headers=tok).status_code == 400


def test_listagem_com_filtro_pagina(ambiente):
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    r = cliente.get("/cep/v2/enderecos", headers=tok, params={"uf": "SP"})
    assert r.status_code == 200
    assert r.json()["page"]["totalElements"] == 1


def test_uf_invalida_da_400(ambiente):
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    assert cliente.get("/cep/v1/ufs/XYZ", headers=tok).status_code == 400


# -- 501 --------------------------------------------------------------------

@pytest.mark.parametrize("rota", [
    "/cep/v1/localidades/cliques",
    "/cep/v1/localidades/caixas-postais",
    "/cep/v1/localidades/agencias-modulares",
])
def test_recurso_sem_dado_da_501_e_nao_e_confundido_com_uf(ambiente, rota):
    """As três rotas são registradas ANTES de `/cep/v1/localidades/{uf}`.

    Sem essa ordem, o Starlette casaria `cliques` como sigla de UF e o cliente
    receberia 400 "UF inválida" em vez do 501 que explica o que falta.
    """
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    r = cliente.get(rota, headers=tok)
    assert r.status_code == 501
    assert "atributo de serviço" in r.json()["message"]


def test_filtro_nao_alimentavel_da_501_nomeando_o_filtro(ambiente):
    """Ignorar o filtro e devolver 200 seria pior: o cliente acharia que
    filtrou."""
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    r = cliente.get("/cep/v2/enderecos", headers=tok,
                    params={"uf": "SP", "numero": "10"})
    assert r.status_code == 501
    assert "numero" in r.json()["message"]


def test_501_tambem_exige_token(ambiente):
    _, cliente, _, _ = ambiente
    assert cliente.get("/cep/v1/localidades/cliques").status_code == 401


# -- saúde ------------------------------------------------------------------

def test_saude_nao_exige_token_e_fica_fora_do_prefixo(ambiente):
    _, cliente, _, _ = ambiente
    r = cliente.get("/saude")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_saude_com_base_inacessivel_devolve_503_e_nao_derruba(ambiente):
    """Sonda de vivacidade REPORTA que o banco caiu; não morre com ele."""
    _, cliente, _, _ = ambiente

    class Morta:
        def executar(self, _):
            raise RuntimeError("base fora")

    cliente.app.state.conexao = Morta()
    r = cliente.get("/saude")
    assert r.status_code == 503
    assert r.json() == {"status": "base inacessível"}


def test_atualizacao_reporta_a_competencia(ambiente):
    _, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    r = cliente.get("/cep/v1/atualizacao", headers=tok)
    assert r.json()["competencia"] == "2026-10"


# -- log --------------------------------------------------------------------

def test_consulta_e_registrada_mesmo_em_erro(ambiente):
    """O registro no `finally` é o que permite separar consulta faturável de
    erro de cliente."""
    cfg, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    cliente.get(f"/cep/v2/enderecos/{CEP}", headers=tok)
    cliente.get("/cep/v2/enderecos/abc", headers=tok)
    cliente.get(f"/cep/v2/enderecos/{CEP_INEXISTENTE}", headers=tok)

    linhas = _linhas_do_log(cfg)
    assert len(linhas) == 3
    por_status = {l["status_http"]: l for l in linhas}
    assert set(por_status) == {200, 400, 404}
    # 200 e 404 são faturáveis; 400 (erro do cliente) não.
    assert por_status[200]["faturavel"] is True
    assert por_status[404]["faturavel"] is True
    assert por_status[400]["faturavel"] is False


def test_o_cep_consultado_nunca_vai_para_o_log(ambiente):
    """Decisão, não omissão — e por isso travada aqui.

    `uso.Log.registrar` aceita um `ni=`, e o molde (CNPJ-XRay) o preenche com o
    CNPJ consultado. Aqui fica de fora: um CNPJ identifica uma empresa, um CEP
    identifica onde alguém mora. Registrar quais CEPs cada cliente consulta é
    montar um histórico de endereços pesquisados.

    O teste cobre as duas portas por onde o CEP poderia escapar: o campo `ni` e
    o campo `rota`.
    """
    cfg, cliente, cred, segredo = ambiente
    tok = _bearer(cliente, cred, segredo)
    cliente.get(f"/cep/v2/enderecos/{CEP}", headers=tok)

    linha = _linhas_do_log(cfg)[0]
    assert linha["ni"] is None
    # A rota é o PADRÃO, não o caminho concreto. Sem isto, agrupar por rota
    # viraria uma chave por CEP consultado.
    assert linha["rota"] == "/cep/v2/enderecos/{cep}"
    # Cinto e suspensório: o CEP não aparece em lugar nenhum da linha.
    assert CEP not in json.dumps(linha)


# -- separação das aplicações ----------------------------------------------

def test_manager_nao_existe_na_aplicacao_publica(ambiente):
    """Uma credencial capaz de criar credenciais seria escalada de privilégio."""
    _, cliente, _, _ = ambiente
    assert cliente.get("/manager/credenciais").status_code == 404
