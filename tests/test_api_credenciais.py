"""Testes do armazenamento de credenciais.

O que importa aqui não é o CRUD — é que as três promessas de segurança do
módulo se sustentem: o segredo em claro não fica guardado, revogar não apaga
histórico, e `autenticar` não conta a quem tenta adivinhar qual das três
condições falhou.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.credenciais import (  # noqa: E402
    Credenciais,
    CredencialNaoEncontrada,
)


@pytest.fixture
def cofre(tmp_path):
    return Credenciais(tmp_path / "credenciais.db")


def test_criar_devolve_o_segredo_uma_vez(cofre):
    cred, secret = cofre.criar("ACME Ltda", "11222333000181", "ti@acme.com.br")

    assert cred.consumer_key and secret
    assert cred.consumer_key != secret
    assert cred.status == "ativa"
    assert cred.revogada_em is None


def test_o_segredo_em_claro_nao_fica_no_arquivo(cofre, tmp_path):
    """A promessa central. Se isto falhar, vazar o volume entrega as credenciais."""
    _, secret = cofre.criar("ACME Ltda")

    bruto = (tmp_path / "credenciais.db").read_bytes()
    assert secret.encode() not in bruto, "o segredo em claro foi parar no arquivo"


def test_duas_credenciais_nunca_saem_iguais(cofre):
    a, sa = cofre.criar("A")
    b, sb = cofre.criar("B")
    assert a.consumer_key != b.consumer_key
    assert sa != sb


def test_o_mesmo_segredo_gera_hash_diferente_em_credenciais_diferentes(cofre, tmp_path):
    """Salt por credencial: hash igual permitiria descobrir reuso de segredo."""
    cofre.criar("A")
    cofre.criar("B")
    con = sqlite3.connect(tmp_path / "credenciais.db")
    hashes = [r[0] for r in con.execute("SELECT secret_hash FROM credencial")]
    salts = [r[0] for r in con.execute("SELECT secret_salt FROM credencial")]
    con.close()
    assert len(set(salts)) == 2
    assert len(set(hashes)) == 2


def test_autenticar_aceita_o_par_correto(cofre):
    cred, secret = cofre.criar("ACME Ltda")
    assert cofre.autenticar(cred.consumer_key, secret).consumer_key == cred.consumer_key


def test_autenticar_recusa_segredo_errado(cofre):
    cred, _ = cofre.criar("ACME Ltda")
    assert cofre.autenticar(cred.consumer_key, "errado" * 4) is None


def test_autenticar_recusa_chave_inexistente(cofre):
    assert cofre.autenticar("naoexiste", "qualquer") is None


def test_autenticar_recusa_credencial_revogada(cofre):
    """Revogação tem que valer na autenticação, não só no relatório."""
    cred, secret = cofre.criar("ACME Ltda")
    cofre.revogar(cred.consumer_key)

    assert cofre.autenticar(cred.consumer_key, secret) is None


def test_autenticar_recusa_credencial_suspensa(cofre):
    cred, secret = cofre.criar("ACME Ltda")
    cofre.alterar(cred.consumer_key, status="suspensa")

    assert cofre.autenticar(cred.consumer_key, secret) is None


def test_revogar_nao_apaga(cofre):
    """O uso histórico precisa continuar ligado à credencial; DELETE deixaria
    buraco no relatório do mês."""
    cred, _ = cofre.criar("ACME Ltda")

    revogada = cofre.revogar(cred.consumer_key)

    assert revogada.status == "revogada"
    assert revogada.revogada_em is not None
    assert cofre.obter(cred.consumer_key).consumer_key == cred.consumer_key
    assert len(cofre.listar()) == 1


def test_rotacionar_troca_o_segredo_e_mantem_a_chave(cofre):
    cred, antigo = cofre.criar("ACME Ltda")

    novo = cofre.rotacionar(cred.consumer_key)

    assert novo != antigo
    assert cofre.autenticar(cred.consumer_key, antigo) is None
    assert cofre.autenticar(cred.consumer_key, novo) is not None
    assert cofre.obter(cred.consumer_key).criada_em == cred.criada_em


def test_alterar_dados_do_contratante_e_quota(cofre):
    cred, _ = cofre.criar("ACME Ltda", quota_mensal=1000)

    alterada = cofre.alterar(
        cred.consumer_key, contratante_email="novo@acme.com.br", quota_mensal=5000
    )

    assert alterada.contratante_email == "novo@acme.com.br"
    assert alterada.quota_mensal == 5000
    assert alterada.contratante_nome == "ACME Ltda"


def test_alterar_recusa_campo_desconhecido(cofre):
    cred, _ = cofre.criar("ACME Ltda")
    with pytest.raises(ValueError, match="não alterável"):
        cofre.alterar(cred.consumer_key, secret_hash="tentativa")


def test_alterar_recusa_status_invalido(cofre):
    cred, _ = cofre.criar("ACME Ltda")
    with pytest.raises(ValueError, match="status inválido"):
        cofre.alterar(cred.consumer_key, status="qualquer")


def test_operacoes_em_chave_inexistente_levantam(cofre):
    for op in (cofre.obter, cofre.revogar, cofre.rotacionar):
        with pytest.raises(CredencialNaoEncontrada):
            op("naoexiste")


def test_listar_filtra_por_status(cofre):
    a, _ = cofre.criar("A")
    cofre.criar("B")
    cofre.revogar(a.consumer_key)

    assert len(cofre.listar()) == 2
    assert len(cofre.listar(status="ativa")) == 1
    assert len(cofre.listar(status="revogada")) == 1


def test_json_nao_expoe_hash_nem_salt(cofre):
    """A representação que vai para o /manager não pode carregar material
    criptográfico."""
    cred, _ = cofre.criar("ACME Ltda")

    j = cred.para_json()

    texto = str(j)
    assert "hash" not in texto.lower()
    assert "salt" not in texto.lower()
    assert j["consumerKey"] == cred.consumer_key
