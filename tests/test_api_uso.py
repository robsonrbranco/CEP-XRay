"""Testes do log de consultas e da consolidação de estatísticas.

Três propriedades importam mais que o resto:

* **a consolidação é idempotente** — rodar duas vezes não conta duas vezes, e
  isso tem que valer porque ela vai rodar por agendamento E sob demanda;
* **o sintético sobrevive à poda do analítico** — é a razão de existirem dois
  níveis;
* **`faturavel` segue a regra do SERPRO**, senão o uso do contratante é inflado
  com erros que não são dele.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.uso import Estatisticas, Log, faturavel  # noqa: E402


@pytest.fixture
def ambiente(tmp_path):
    return {
        "logs": tmp_path / "logs",
        "log": Log(tmp_path / "logs", pid=1234),
        "stats": Estatisticas(tmp_path / "estatisticas.db"),
    }


def _envelhecer(diretorio: Path, dia: str) -> None:
    """Renomeia os arquivos de hoje para um dia passado, para a consolidação
    considerá-los fechados."""
    for arq in diretorio.glob("consultas-*.jsonl"):
        pid = arq.stem.split("-")[1]
        arq.rename(diretorio / f"consultas-{pid}-{dia}.jsonl")


# ---------------------------------------------------------------------------
# faturável
# ---------------------------------------------------------------------------

def test_faturavel_segue_a_regra_do_serpro():
    for ok in (200, 206, 404):
        assert faturavel(ok), f"{ok} deveria ser faturável"
    for nao in (400, 401, 403, 500, 502, 504):
        assert not faturavel(nao), f"{nao} não deveria ser faturável"


def test_404_e_faturavel_e_400_nao():
    """A distinção que justifica validar o DV: CNPJ inexistente é consulta
    prestada; CNPJ malformado é erro do cliente."""
    assert faturavel(404)
    assert not faturavel(400)


# ---------------------------------------------------------------------------
# log analítico
# ---------------------------------------------------------------------------

def test_registrar_grava_uma_linha_por_consulta(ambiente):
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 12, ni="11222333000181")
    ambiente["log"].registrar("KEY1", "/v2/qsa", 200, 30)

    arqs = ambiente["log"].arquivos()
    assert len(arqs) == 1
    linhas = arqs[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(linhas) == 2
    assert json.loads(linhas[0])["ni"] == "11222333000181"
    assert json.loads(linhas[1])["ni"] is None


def test_cada_worker_escreve_no_proprio_arquivo(tmp_path):
    """É o que dispensa lock: sem arquivo compartilhado, não há concorrência."""
    Log(tmp_path, pid=1).registrar("K", "/v2/basica", 200, 5)
    Log(tmp_path, pid=2).registrar("K", "/v2/basica", 200, 5)

    assert len(sorted(tmp_path.glob("consultas-*.jsonl"))) == 2


def test_linha_do_log_traz_o_faturavel_resolvido(ambiente):
    ambiente["log"].registrar("KEY1", "/v2/basica", 401, 1)

    linha = json.loads(ambiente["log"].arquivos()[0].read_text(encoding="utf-8").strip())
    assert linha["faturavel"] is False


# ---------------------------------------------------------------------------
# consolidação
# ---------------------------------------------------------------------------

def test_consolidar_soma_no_sintetico(ambiente):
    for _ in range(3):
        ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    ambiente["log"].registrar("KEY1", "/v2/qsa", 404, 5)
    _envelhecer(ambiente["logs"], "2026-09-01")

    r = ambiente["stats"].consolidar(ambiente["logs"])

    assert r == {"arquivos": 1, "linhas": 4}
    resumo = ambiente["stats"].resumo()
    assert resumo["consultas"] == 4
    assert resumo["porRota"] == {"/v2/basica": 3, "/v2/qsa": 1}


def test_consolidar_duas_vezes_nao_conta_duas_vezes(ambiente):
    """Idempotência: a consolidação roda por agendamento E sob demanda."""
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    _envelhecer(ambiente["logs"], "2026-09-01")

    ambiente["stats"].consolidar(ambiente["logs"])
    segunda = ambiente["stats"].consolidar(ambiente["logs"])

    assert segunda == {"arquivos": 0, "linhas": 0}
    assert ambiente["stats"].resumo()["consultas"] == 1


def test_consolidar_ignora_o_arquivo_de_hoje(ambiente):
    """Arquivo vivo ainda recebe escrita; lê-lo exigiria controlar deslocamento."""
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)

    r = ambiente["stats"].consolidar(ambiente["logs"])

    assert r == {"arquivos": 0, "linhas": 0}
    assert ambiente["stats"].consolidar(ambiente["logs"], incluir_hoje=True)["linhas"] == 1


def test_linha_truncada_nao_aborta_a_consolidacao(ambiente):
    """Queda no meio da escrita deixa lixo na última linha. Perder um registro
    de uso é melhor que perder a consolidação inteira."""
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    _envelhecer(ambiente["logs"], "2026-09-01")
    arq = ambiente["log"].arquivos()[0]
    with open(arq, "a", encoding="utf-8") as f:
        f.write('{"instante": "2026-09-01T00:00:00+00:00", "consumer_k')

    r = ambiente["stats"].consolidar(ambiente["logs"])

    assert r["linhas"] == 1


def test_consumo_do_mes_conta_so_faturavel(ambiente):
    for _ in range(5):
        ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    for _ in range(3):
        ambiente["log"].registrar("KEY1", "/v2/basica", 401, 1)
    _envelhecer(ambiente["logs"], "2026-09-15")
    ambiente["stats"].consolidar(ambiente["logs"])

    assert ambiente["stats"].consumo_do_mes("KEY1", "2026-09") == 5


def test_resumo_por_credencial_isola(ambiente):
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    ambiente["log"].registrar("KEY2", "/v2/basica", 200, 10)
    ambiente["log"].registrar("KEY2", "/v2/basica", 200, 10)
    _envelhecer(ambiente["logs"], "2026-09-01")
    ambiente["stats"].consolidar(ambiente["logs"])

    assert ambiente["stats"].resumo("KEY1")["consultas"] == 1
    assert ambiente["stats"].resumo("KEY2")["consultas"] == 2
    assert ambiente["stats"].resumo()["consultas"] == 3


def test_resumo_traz_duracao_media(ambiente):
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 20)
    _envelhecer(ambiente["logs"], "2026-09-01")
    ambiente["stats"].consolidar(ambiente["logs"])

    assert ambiente["stats"].resumo("KEY1")["duracaoMediaMs"] == 15.0


def test_resumo_de_credencial_sem_uso_nao_quebra(ambiente):
    r = ambiente["stats"].resumo("NUNCA_USADA")
    assert r["consultas"] == 0
    assert r["duracaoMediaMs"] == 0.0


# ---------------------------------------------------------------------------
# poda
# ---------------------------------------------------------------------------

def test_poda_apaga_o_analitico_e_preserva_o_sintetico(ambiente):
    """A assimetria que justifica os dois níveis: o detalhe do que foi
    consultado é descartável; a estatística de uso, não."""
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    _envelhecer(ambiente["logs"], "2020-01-01")
    ambiente["stats"].consolidar(ambiente["logs"])

    apagados = ambiente["stats"].podar(ambiente["logs"], dias=90)

    assert apagados == 1
    assert ambiente["log"].arquivos() == []
    assert ambiente["stats"].resumo()["consultas"] == 1


def test_poda_nao_apaga_o_que_ainda_nao_foi_consolidado(ambiente):
    """Apagar antes de consolidar perderia o uso para sempre."""
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    _envelhecer(ambiente["logs"], "2020-01-01")

    apagados = ambiente["stats"].podar(ambiente["logs"], dias=90)

    assert apagados == 0
    assert len(ambiente["log"].arquivos()) == 1


def test_poda_preserva_arquivo_recente(ambiente):
    ambiente["log"].registrar("KEY1", "/v2/basica", 200, 10)
    _envelhecer(ambiente["logs"], "2099-01-01")
    ambiente["stats"].consolidar(ambiente["logs"])

    assert ambiente["stats"].podar(ambiente["logs"], dias=90) == 0
