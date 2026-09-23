"""A camada MCP (`POST /mcp`), de ponta a ponta, sem Firebird e sem rede.

O que se defende aqui, em ordem de quanto custaria errar:

1. **As duas portas não se misturam.** O token de 90 dias do MCP não pode valer
   nas rotas REST, e o token de uma hora do REST não pode valer no MCP.
2. **Revogar e rotacionar derrubam o token de longa duração** — é o que torna
   aceitável ele viver meses.
3. **A cobrança bate com a do REST**, e o CEP consultado não vai para o log.
4. O protocolo: o subconjunto que `api/mcp.py` declara, e as recusas dele.
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api import token as mod_token  # noqa: E402
from src.api.config import ConfigAPI  # noqa: E402
from src.api.credenciais import Credenciais  # noqa: E402
from src.api.manager import criar_app as criar_manager  # noqa: E402
from src.api.publica import criar_app  # noqa: E402

URI = "https://hestia.ecomciencia.com/mcp"
CEP = "01001000"
CEP_INEXISTENTE = "00000000"


class ConsultasFalsas:
    def __init__(self):
        self.falhar = False
        self.chamadas_atualizacao = 0

    def _talvez_falhar(self):
        if self.falhar:
            raise RuntimeError("attachment perdido")

    def cep(self, valor):
        self._talvez_falhar()
        if valor == CEP_INEXISTENTE:
            return None
        return {
            "cep": valor, "origem": "logradouro", "tipo": "Praça",
            "nome_logradouro": "da Sé", "estado": "SP",
            "bairro": {"id": 1, "nome": "Sé"},
            "cidade": {"id": 1, "nome": "São Paulo"},
            "complemento": "lado ímpar", "grande_usuario": None,
        }

    def enderecos(self, **kw):
        self._talvez_falhar()
        self.ultimos_filtros = kw
        return [self.cep(CEP)], 1

    def localidades(self, **kw):
        self._talvez_falhar()
        if kw.get("uf") and kw["uf"] != "SP":
            return [], 0
        return [{"id_cidade": 1, "cidade": "São Paulo", "estado": "SP",
                 "cidade_ibge": "3550308", "ddd": "11",
                 "latitude": None, "longitude": None}], 1

    def atualizacao(self):
        self.chamadas_atualizacao += 1
        self._talvez_falhar()
        return {"competencia": "2026-10",
                "atualizadoEm": "2026-09-21T15:53:47+00:00",
                "totalRegistros": 2171508}


@pytest.fixture
def amb(tmp_path):
    cfg = ConfigAPI(
        credenciais=tmp_path / "creds" / "credenciais.db",
        logs=tmp_path / "logs",
        estatisticas=tmp_path / "creds" / "estatisticas.db",
        jwt_segredo="segredo-de-teste",
        mcp_uri=URI,
    )
    creds = Credenciais(cfg.credenciais)
    cred, segredo = creds.criar(contratante_nome="Agente de teste")
    consultas = ConsultasFalsas()
    api = TestClient(criar_app(cfg, consultas=consultas),
                     raise_server_exceptions=False)
    manager = TestClient(criar_manager(cfg), raise_server_exceptions=False)

    class Amb:
        pass

    a = Amb()
    a.cfg, a.creds, a.cred, a.segredo = cfg, creds, cred, segredo
    a.consultas, a.api, a.manager = consultas, api, manager
    return a


def _token_mcp(a, dias=90):
    r = a.manager.post(f"/manager/credenciais/{a.cred.consumer_key}/token-mcp",
                       params={"dias": dias})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _cab(tok, **extra):
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"}
    h.update(extra)
    return h


def _rpc(a, tok, metodo, params=None, id_=1, **cab):
    corpo = {"jsonrpc": "2.0", "id": id_, "method": metodo}
    if params is not None:
        corpo["params"] = params
    return a.api.post("/mcp", headers=_cab(tok, **cab), json=corpo)


def _chamar(a, tok, nome, args):
    r = _rpc(a, tok, "tools/call", {"name": nome, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _log(a):
    linhas = []
    for f in sorted(a.cfg.logs.glob("consultas-*.jsonl")):
        linhas += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    return linhas


def _token_rest(a):
    b = base64.b64encode(f"{a.cred.consumer_key}:{a.segredo}".encode()).decode()
    r = a.api.post("/token/v1/autentica", headers={"Authorization": f"Basic {b}"})
    assert r.status_code == 201
    return r.json()["token"]


# -- 1. as duas portas não se misturam --------------------------------------

def test_token_mcp_nao_vale_nas_rotas_rest(amb):
    r = amb.api.get(f"/cep/v2/enderecos/{CEP}",
                    headers={"Authorization": f"Bearer {_token_mcp(amb)}"})
    assert r.status_code == 401


def test_token_rest_nao_vale_no_mcp(amb):
    r = _rpc(amb, _token_rest(amb), "tools/list")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_token_de_outra_audiencia_e_recusado(amb):
    geracao = amb.creds.geracao(amb.cred.consumer_key)
    tok, _ = mod_token.emitir(amb.cred.consumer_key, amb.cfg.jwt_segredo,
                              audiencia="https://themis.ecomciencia.com/mcp",
                              geracao=geracao)
    assert _rpc(amb, tok, "tools/list").status_code == 401


def test_sem_token_da_401(amb):
    r = amb.api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_o_token_rest_continua_funcionando(amb):
    """A recusa de `aud` no REST não pode ter quebrado o token de sempre."""
    r = amb.api.get(f"/cep/v2/enderecos/{CEP}",
                    headers={"Authorization": f"Bearer {_token_rest(amb)}"})
    assert r.status_code == 200


# -- 2. revogar e rotacionar derrubam o token de longa duração ---------------

def test_revogar_derruba_o_token_mcp(amb):
    tok = _token_mcp(amb)
    assert _rpc(amb, tok, "ping").status_code == 200
    amb.manager.delete(f"/manager/credenciais/{amb.cred.consumer_key}")
    assert _rpc(amb, tok, "ping").status_code == 401


def test_rotacionar_o_segredo_derruba_o_token_mcp(amb):
    tok = _token_mcp(amb)
    assert _rpc(amb, tok, "ping").status_code == 200
    amb.manager.post(f"/manager/credenciais/{amb.cred.consumer_key}/rotacionar")
    r = _rpc(amb, tok, "ping")
    assert r.status_code == 401
    assert "rotacionado" in r.json()["message"]
    # E um token novo, emitido depois da rotação, volta a funcionar.
    assert _rpc(amb, _token_mcp(amb), "ping").status_code == 200


def test_token_expirado_e_recusado(amb):
    geracao = amb.creds.geracao(amb.cred.consumer_key)
    tok, _ = mod_token.emitir(amb.cred.consumer_key, amb.cfg.jwt_segredo,
                              validade_s=10, agora=1_000_000,
                              audiencia=URI, geracao=geracao)
    assert _rpc(amb, tok, "ping").status_code == 401


# -- emissão pelo /manager ---------------------------------------------------

def test_manager_emite_token_com_audiencia_e_geracao(amb):
    r = amb.manager.post(f"/manager/credenciais/{amb.cred.consumer_key}/token-mcp",
                         params={"dias": 30})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["audiencia"] == URI
    claims = mod_token.verificar(corpo["token"], amb.cfg.jwt_segredo)
    assert claims["aud"] == URI
    assert claims["scope"] == "mcp"
    assert claims["gen"] == amb.creds.geracao(amb.cred.consumer_key)
    assert claims["exp"] - claims["iat"] == 30 * 86400


@pytest.mark.parametrize("dias", [0, 366, -1])
def test_manager_recusa_validade_fora_da_faixa(amb, dias):
    r = amb.manager.post(f"/manager/credenciais/{amb.cred.consumer_key}/token-mcp",
                         params={"dias": dias})
    assert r.status_code == 400


def test_manager_recusa_credencial_revogada(amb):
    amb.manager.delete(f"/manager/credenciais/{amb.cred.consumer_key}")
    r = amb.manager.post(f"/manager/credenciais/{amb.cred.consumer_key}/token-mcp")
    assert r.status_code == 400


def test_manager_recusa_sem_mcp_configurado(tmp_path):
    cfg = ConfigAPI(credenciais=tmp_path / "c.db", logs=tmp_path / "l",
                    estatisticas=tmp_path / "e.db", jwt_segredo="x")
    cred, _ = Credenciais(cfg.credenciais).criar(contratante_nome="X")
    manager = TestClient(criar_manager(cfg))
    r = manager.post(f"/manager/credenciais/{cred.consumer_key}/token-mcp")
    assert r.status_code == 400


def test_sem_mcp_configurado_a_rota_nao_existe(tmp_path):
    cfg = ConfigAPI(credenciais=tmp_path / "c.db", logs=tmp_path / "l",
                    estatisticas=tmp_path / "e.db", jwt_segredo="x")
    api = TestClient(criar_app(cfg, consultas=ConsultasFalsas()))
    assert api.post("/mcp", json={}).status_code == 404


# -- 3. cobrança e privacidade do log ----------------------------------------

def test_consulta_gera_linha_faturavel_sem_o_cep(amb):
    tok = _token_mcp(amb)
    _chamar(amb, tok, "consultar_cep", {"cep": "01001-000"})
    linhas = _log(amb)
    assert len(linhas) == 1
    l = linhas[0]
    assert l["rota"] == "mcp:consultar_cep"
    assert l["status_http"] == 200 and l["faturavel"] is True
    # Um CEP identifica onde alguém mora: não entra no log, por nenhum campo.
    assert CEP not in json.dumps(l) and "01001-000" not in json.dumps(l)


def test_descoberta_nao_gera_linha_de_log(amb):
    tok = _token_mcp(amb)
    _rpc(amb, tok, "initialize", {"protocolVersion": "2025-06-18",
                                  "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    _rpc(amb, tok, "tools/list")
    _rpc(amb, tok, "resources/list")
    _rpc(amb, tok, "resources/read", {"uri": "hestia://competencia"})
    assert _log(amb) == []


def test_cep_malformado_nao_e_faturavel(amb):
    res = _chamar(amb, _token_mcp(amb), "consultar_cep", {"cep": "123"})
    assert res["isError"] is True
    assert _log(amb)[0]["status_http"] == 400
    assert _log(amb)[0]["faturavel"] is False


def test_nao_encontrado_e_resultado_e_e_faturavel_como_no_rest(amb):
    res = _chamar(amb, _token_mcp(amb), "consultar_cep", {"cep": CEP_INEXISTENTE})
    assert res["isError"] is False
    assert res["structuredContent"]["encontrado"] is False
    assert res["structuredContent"]["competencia"] == "2026-10"
    assert _log(amb)[0]["status_http"] == 404
    assert _log(amb)[0]["faturavel"] is True


def test_base_fora_do_ar_e_falha_legivel_e_nao_faturavel(amb):
    tok = _token_mcp(amb)
    amb.consultas.falhar = True
    res = _chamar(amb, tok, "consultar_cep", {"cep": CEP})
    assert res["isError"] is True
    assert "DESCONHECIDO" in res["content"][0]["text"]
    assert _log(amb)[0]["status_http"] == 500
    assert _log(amb)[0]["faturavel"] is False


def test_cota_esgotada_vira_falha_de_ferramenta_sem_log(amb):
    tok = _token_mcp(amb)
    amb.creds.alterar(amb.cred.consumer_key, quota_mensal=0)
    # Descobrir ferramentas continua funcionando com a cota esgotada...
    assert _rpc(amb, tok, "tools/list").status_code == 200
    # ...e a recusa chega ao modelo como texto que ele consegue ler.
    res = _chamar(amb, tok, "consultar_cep", {"cep": CEP})
    assert res["isError"] is True
    assert "Quota" in res["content"][0]["text"]
    assert _log(amb) == []


# -- as ferramentas ----------------------------------------------------------

def test_consultar_cep_devolve_recorte_enxuto(amb):
    res = _chamar(amb, _token_mcp(amb), "consultar_cep", {"cep": CEP})
    d = res["structuredContent"]
    assert d["encontrado"] is True
    assert d["localidade"] == "São Paulo" and d["logradouro"] == "da Sé"
    assert d["tipoCEP"] == "logradouro"
    assert d["lado"] == "ímpar"
    assert d["competencia"] == "2026-10"
    # Os campos que o contrato dos Correios manda sempre nulos não viajam.
    for campo in ("numeroLocalidade", "abreviatura", "cepUnidadeOperacional",
                  "numeroInicial", "numeroFinal", "nomeUnidade"):
        assert campo not in d
    # E o mesmo conteúdo vai em texto, para cliente que não lê structuredContent.
    assert json.loads(res["content"][0]["text"]) == d


def test_buscar_enderecos_exige_filtro(amb):
    res = _chamar(amb, _token_mcp(amb), "buscar_enderecos", {})
    assert res["isError"] is True
    assert "filtro" in res["content"][0]["text"]


def test_buscar_enderecos_repassa_filtros_e_limita_pagina(amb):
    tok = _token_mcp(amb)
    res = _chamar(amb, tok, "buscar_enderecos",
                  {"uf": "sp", "localidade_id": 1, "logradouro": "Sé", "tamanho": 5})
    assert res["isError"] is False
    assert amb.consultas.ultimos_filtros["uf"] == "SP"
    assert amb.consultas.ultimos_filtros["id_localidade"] == 1
    assert res["structuredContent"]["total"] == 1
    grande = _chamar(amb, tok, "buscar_enderecos", {"uf": "SP", "tamanho": 51})
    assert grande["isError"] is True


@pytest.mark.parametrize("args", [
    {"uf": "SP", "pagina": "0"},
    {"uf": "SP", "pagina": True},
    {"uf": "São Paulo"},
    {"uf": "SP", "desconhecido": 1},
])
def test_argumento_errado_e_falha_que_o_modelo_le(amb, args):
    res = _chamar(amb, _token_mcp(amb), "buscar_enderecos", args)
    assert res["isError"] is True


def test_listar_localidades(amb):
    tok = _token_mcp(amb)
    res = _chamar(amb, tok, "listar_localidades", {"uf": "SP"})
    item = res["structuredContent"]["itens"][0]
    assert item == {"id": 1, "nome": "São Paulo", "uf": "SP",
                    "codigoIbge": "3550308", "ddd": "11"}
    assert _chamar(amb, tok, "listar_localidades", {})["isError"] is True


def test_competencia_e_lida_uma_vez_por_processo(amb):
    tok = _token_mcp(amb)
    for _ in range(3):
        _chamar(amb, tok, "consultar_cep", {"cep": CEP})
    assert amb.consultas.chamadas_atualizacao == 1


# -- 4. o protocolo ----------------------------------------------------------

def test_initialize_negocia_versao_e_nao_abre_sessao(amb):
    tok = _token_mcp(amb)
    r = _rpc(amb, tok, "initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert set(res["capabilities"]) == {"tools", "resources"}
    assert "2026-10" in res["instructions"]
    assert "mcp-session-id" not in {k.lower() for k in r.headers}


def test_initialize_com_versao_desconhecida_devolve_a_mais_nova(amb):
    r = _rpc(amb, _token_mcp(amb), "initialize", {"protocolVersion": "2030-01-01"})
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_cabecalho_de_versao_desconhecido_da_400(amb):
    r = _rpc(amb, _token_mcp(amb), "ping", **{"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 400


def test_tools_list_declara_somente_leitura(amb):
    ferramentas = _rpc(amb, _token_mcp(amb), "tools/list").json()["result"]["tools"]
    assert {f["name"] for f in ferramentas} == {
        "consultar_cep", "buscar_enderecos", "listar_localidades"}
    for f in ferramentas:
        assert f["annotations"]["readOnlyHint"] is True
        assert f["inputSchema"]["type"] == "object"


def test_notificacao_devolve_202_sem_corpo(amb):
    r = amb.api.post("/mcp", headers=_cab(_token_mcp(amb)),
                     json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content == b""


def test_lote_e_recusado(amb):
    r = amb.api.post("/mcp", headers=_cab(_token_mcp(amb)),
                     json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600


def test_json_invalido(amb):
    r = amb.api.post("/mcp", headers=_cab(_token_mcp(amb)), content=b"{nao e json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_ferramenta_e_metodo_desconhecidos(amb):
    tok = _token_mcp(amb)
    assert _rpc(amb, tok, "tools/call", {"name": "apagar_tudo"}).json()["error"]["code"] == -32602
    assert _rpc(amb, tok, "sampling/createMessage").json()["error"]["code"] == -32601


def test_recurso_competencia(amb):
    tok = _token_mcp(amb)
    r = _rpc(amb, tok, "resources/read", {"uri": "hestia://competencia"})
    conteudo = r.json()["result"]["contents"][0]
    assert json.loads(conteudo["text"])["competencia"] == "2026-10"
    assert _rpc(amb, tok, "resources/read", {"uri": "file:///etc/passwd"}).json()["error"]["code"] == -32002


def test_get_e_delete_devolvem_405(amb):
    tok = _token_mcp(amb)
    assert amb.api.get("/mcp", headers=_cab(tok)).status_code == 405
    assert amb.api.delete("/mcp", headers=_cab(tok)).status_code == 405


def test_origem_de_navegador_estranha_e_recusada(amb):
    tok = _token_mcp(amb)
    r = _rpc(amb, tok, "ping", Origin="https://atacante.example")
    assert r.status_code == 403
    # A própria origem do serviço é aceita.
    assert _rpc(amb, tok, "ping", Origin="https://hestia.ecomciencia.com").status_code == 200


def test_mcp_fica_fora_do_openapi(amb):
    """O OpenAPI documenta o contrato dos Correios; o MCP é contrato nosso."""
    assert "/mcp" not in amb.api.get("/openapi.json").json()["paths"]
