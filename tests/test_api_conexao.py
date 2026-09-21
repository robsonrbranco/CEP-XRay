"""Testes da conexão viva.

A conexão é aberta uma vez e segurada pela vida do worker, porque abrir
attachment em Firebird embedded custa ~387 ms contra 0,4 ms da consulta. O que
precisa ser garantido é que segurar não traga os problemas de segurar: conexão
morta que não se recupera, e acesso concorrente sem proteção.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.conexao import ConexaoViva  # noqa: E402


class ConexaoFalsa:
    def __init__(self, nome):
        self.nome = nome
        self.fechada = False


@pytest.fixture
def viva(monkeypatch):
    """`ConexaoViva` com a abertura real trocada por um contador."""
    v = ConexaoViva()
    v.aberturas = 0

    def abrir(self=v):
        self.aberturas += 1
        self._ctx = type("Ctx", (), {"__exit__": lambda *a: None})()
        return ConexaoFalsa(f"con{self.aberturas}")

    monkeypatch.setattr(v, "_abrir", abrir)
    return v


def test_abre_uma_vez_e_reaproveita(viva):
    """O ponto inteiro do módulo: 100 consultas, um attachment."""
    for _ in range(100):
        viva.executar(lambda con: con.nome)

    assert viva.aberturas == 1


def test_devolve_o_resultado_da_funcao(viva):
    assert viva.executar(lambda con: 42) == 42


def test_reabre_uma_vez_quando_a_conexao_cai(viva):
    """Conexão de vida longa morre — o arquivo some no volume, o engine
    derruba. Sem reabertura o worker ficaria inútil até reiniciar."""
    chamadas = []

    def falha_na_primeira(con):
        chamadas.append(con.nome)
        if len(chamadas) == 1:
            raise OSError("conexão morta")
        return con.nome

    resultado = viva.executar(falha_na_primeira)

    assert viva.aberturas == 2
    assert resultado == "con2"


def test_nao_insiste_indefinidamente(viva):
    """Reabrir mais de uma vez esconderia base ausente atrás de latência
    crescente, em vez de deixar o erro aparecer."""
    def sempre_falha(con):
        raise OSError("base sumiu")

    with pytest.raises(OSError, match="base sumiu"):
        viva.executar(sempre_falha)

    assert viva.aberturas == 2


def test_acesso_concorrente_e_serializado(viva):
    """As rotas são `async def` hoje e rodam serialmente, mas trocar uma para
    `def` faz o FastAPI usar um pool de threads — e `Connection` do
    firebird-driver não é thread-safe. O lock remove a armadilha."""
    dentro = []
    maximo = [0]

    def registra(con):
        dentro.append(1)
        maximo[0] = max(maximo[0], len(dentro))
        import time
        time.sleep(0.01)
        dentro.pop()
        return True

    threads = [threading.Thread(target=lambda: viva.executar(registra))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert maximo[0] == 1, "duas threads usaram a conexão ao mesmo tempo"
    assert viva.aberturas == 1


def test_fechar_permite_reabrir(viva):
    viva.executar(lambda con: con.nome)
    viva.fechar()

    viva.executar(lambda con: con.nome)

    assert viva.aberturas == 2


def test_consultar_aceita_conexao_emprestada_e_nao_a_fecha():
    """A conexão emprestada pertence a quem emprestou — fechá-la aqui mataria a
    conexão viva do worker na primeira consulta."""
    from src.consulta.cep import _conexao

    emprestada = ConexaoFalsa("emprestada")
    with _conexao(emprestada) as con:
        assert con is emprestada
    assert not emprestada.fechada
