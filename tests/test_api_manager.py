"""O /manager: gestão de credenciais, em processo e porta separados.

Este arquivo nasceu de um bug real. Ao reescrever `api/esquemas.py` para o
contrato dos Correios, os modelos que só o manager usa ficaram de fora — e a
aplicação do manager parou de nem construir, com `AttributeError` no import.
Nenhum teste pegou porque nenhum teste construía o manager.

O primeiro teste abaixo é, literalmente, "o app monta". Parece trivial e é o
que faltava.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.config import ConfigAPI  # noqa: E402
from src.api.manager import criar_app  # noqa: E402


@pytest.fixture
def cliente(tmp_path):
    cfg = ConfigAPI(
        credenciais=tmp_path / "creds" / "credenciais.db",
        logs=tmp_path / "logs",
        estatisticas=tmp_path / "creds" / "estatisticas.db",
        jwt_segredo="segredo-de-teste",
    )
    return TestClient(criar_app(cfg), raise_server_exceptions=False)


def _criar(cliente, **kw):
    dados = {"contratante_nome": "ACME Logística Ltda"}
    dados.update(kw)
    r = cliente.post("/manager/credenciais", json=dados)
    assert r.status_code == 201, r.text
    return r.json()


def test_a_aplicacao_monta(cliente):
    """A regressão que este arquivo existe para impedir.

    `manager.py` referencia modelos de `esquemas.py` que a API pública não usa.
    Se um deles some, o `criar_app` estoura no import — e sem este teste isso
    só apareceria em produção, na primeira vez que alguém fosse cadastrar um
    consumidor.
    """
    assert cliente.get("/manager/credenciais").status_code == 200


def test_criar_devolve_o_segredo_uma_unica_vez(cliente):
    criada = _criar(cliente)
    assert "consumerSecret" in criada
    assert criada["aviso"]

    consultada = cliente.get(f"/manager/credenciais/{criada['consumerKey']}")
    assert consultada.status_code == 200
    # O segredo NÃO volta: só o hash é guardado.
    assert "consumerSecret" not in consultada.json()


def test_listar_e_filtrar_por_status(cliente):
    a = _criar(cliente)
    _criar(cliente, contratante_nome="Outra Ltda")
    cliente.delete(f"/manager/credenciais/{a['consumerKey']}")

    assert len(cliente.get("/manager/credenciais").json()["credenciais"]) == 2
    ativas = cliente.get("/manager/credenciais", params={"status": "ativa"})
    assert len(ativas.json()["credenciais"]) == 1
    revogadas = cliente.get("/manager/credenciais", params={"status": "revogada"})
    assert len(revogadas.json()["credenciais"]) == 1


def test_status_invalido_da_400(cliente):
    r = cliente.get("/manager/credenciais", params={"status": "inventado"})
    assert r.status_code == 400


def test_revogar_nao_apaga(cliente):
    """O histórico de uso precisa continuar íntegro depois da revogação."""
    criada = _criar(cliente)
    chave = criada["consumerKey"]
    r = cliente.delete(f"/manager/credenciais/{chave}")
    assert r.status_code == 200
    assert r.json()["status"] == "revogada"
    assert r.json()["revogadaEm"]
    # Continua consultável.
    assert cliente.get(f"/manager/credenciais/{chave}").status_code == 200


def test_rotacionar_mantem_a_chave_e_troca_o_segredo(cliente):
    criada = _criar(cliente)
    chave = criada["consumerKey"]
    r = cliente.post(f"/manager/credenciais/{chave}/rotacionar")
    assert r.status_code == 200
    assert r.json()["consumerKey"] == chave
    assert r.json()["consumerSecret"] != criada["consumerSecret"]


def test_alterar_quota(cliente):
    chave = _criar(cliente)["consumerKey"]
    r = cliente.patch(f"/manager/credenciais/{chave}", json={"quota_mensal": 500})
    assert r.status_code == 200
    assert r.json()["quotaMensal"] == 500


def test_alterar_sem_campo_da_400(cliente):
    chave = _criar(cliente)["consumerKey"]
    assert cliente.patch(f"/manager/credenciais/{chave}", json={}).status_code == 400


def test_credencial_inexistente_da_404(cliente):
    assert cliente.get("/manager/credenciais/naoexiste").status_code == 404
    assert cliente.patch(
        "/manager/credenciais/naoexiste", json={"quota_mensal": 1}
    ).status_code == 404
    assert cliente.delete("/manager/credenciais/naoexiste").status_code == 404


def test_estatisticas_respondem_com_base_vazia(cliente):
    """Serviço recém-subido, sem nenhuma consulta ainda, não pode dar 500."""
    r = cliente.get("/manager/estatisticas")
    assert r.status_code == 200
    assert r.json()["consultas"] == 0


def test_podar_sem_dias_da_400(cliente):
    """Não há janela de retenção automática: quem poda informa o prazo.

    Adivinhar um padrão aqui apagaria log que alguém achava que tinha.
    """
    assert cliente.post("/manager/manutencao/podar").status_code == 400
    assert cliente.post(
        "/manager/manutencao/podar", params={"dias": 90}
    ).status_code == 200


def test_o_manager_nao_tem_as_rotas_publicas(cliente):
    """A separação por processo é a barreira que não depende de segredo.

    O par deste teste está em `test_api_rotas.py`, provando o inverso.
    """
    assert cliente.get("/cep/v2/enderecos/01001000").status_code == 404
    assert cliente.post("/token/v1/autentica").status_code == 404
    assert cliente.get("/saude").status_code == 404


def test_especificacao_do_manager_documenta_o_proprio_contrato(cliente):
    spec = cliente.get("/openapi.json").json()
    assert any(c.startswith("/manager/") for c in spec["paths"])
    # Aqui o 422 é legítimo: os corpos são modelos pydantic de verdade,
    # validados. Na API pública ele é removido porque nunca acontece.
    criar = spec["paths"]["/manager/credenciais"]["post"]["responses"]
    assert "422" in criar
    assert "201" in criar
