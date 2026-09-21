"""`criar_app` não depende de quem carregou o ambiente, nem da ordem das rotas.

O bug que este arquivo tranca era assim: `ConfigAPI` cobria JWT e volumes, mas
a configuração do BANCO era lida do `os.environ` lá no fundo, na primeira
consulta. Fora do pod — onde o `Dockerfile` define tudo por `ENV` — isso
tornava a aplicação dependente de um efeito colateral de import:

* primeira requisição em `/cep/v2/enderecos/{cep}` → `Consultas.cep` importa
  `src/consulta/cep.py`, que é TAMBÉM uma CLI e faz `load_dotenv` no topo do
  módulo → o ambiente aparecia bem a tempo → **200**;
* primeira requisição em `/cep/v2/enderecos` (busca) → `Consultas.enderecos`
  importa `src/consulta/listagem.py`, que não é CLI e não faz `load_dotenv` →
  `DB_NAME` vazio → DSN `inet://localhost:3050/` → **500**.

Nenhum teste pegava porque a suíte injeta `consultas=`, e assim nunca abre uma
conexão de verdade através de `criar_app`. É por isso que os testes daqui
olham a CONSTRUÇÃO, não a consulta: é o que dá para falsificar sem Firebird.

E o erro que o usuário via era `Your user name and password are not defined.
Ask your database administrator to set up a Firebird login.` — mandando
procurar servidor, login e administrador que não existem, porque a base é
embedded num arquivo ao lado.
"""

import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import pytest  # noqa: E402

from src.api.config import ConfigAPI  # noqa: E402
from src.api.publica import criar_app  # noqa: E402
from src.db.config import FirebirdConfig  # noqa: E402


def _cfg(tmp_path, **kw):
    return ConfigAPI(
        credenciais=tmp_path / "creds" / "credenciais.db",
        logs=tmp_path / "logs",
        estatisticas=tmp_path / "creds" / "estatisticas.db",
        jwt_segredo="segredo-de-teste",
        **kw,
    )


def test_criar_app_recusa_configuracao_sem_banco(tmp_path):
    """Sem `firebird` e sem dublê, a falha é AGORA e diz o que falta.

    Antes, `criar_app` devolvia uma aplicação de aparência saudável que só
    quebrava na primeira consulta — e com uma mensagem sobre login de banco.
    """
    with pytest.raises(RuntimeError) as e:
        criar_app(_cfg(tmp_path))
    assert "firebird" in str(e.value).lower()


def test_dublê_continua_dispensando_banco(tmp_path):
    """O contrapeso: injetar `consultas=` segue rodando sem Firebird nenhum.

    Se isto quebrar, a guarda acima foi longe demais e levou junto a decisão
    que faz a suíte inteira rodar em menos de um segundo.
    """
    app = criar_app(_cfg(tmp_path), consultas=object())
    assert app.state.conexao is None


def test_a_conexao_recebe_a_configuracao_e_nao_vai_ao_ambiente(tmp_path):
    """É esta a correção de verdade: o `.fdb` sai do `cfg`, não do `os.environ`.

    Enquanto a conexão receber o objeto de configuração, nenhuma ordem de
    importação pode mudar em que base a aplicação vai bater.
    """
    fb = FirebirdConfig(database="/data/cep_xray.fdb", embedded=True)
    app = criar_app(_cfg(tmp_path, firebird=fb))
    assert app.state.conexao._cfg is fb


def test_do_ambiente_sem_db_name_falha_nomeando_db_name(monkeypatch):
    """A guarda que substitui o erro enganoso do driver."""
    monkeypatch.setenv("API_JWT_SEGREDO", "x")
    monkeypatch.delenv("DB_NAME", raising=False)
    with pytest.raises(RuntimeError) as e:
        ConfigAPI.do_ambiente()
    assert "DB_NAME" in str(e.value)


def test_do_ambiente_preenche_o_firebird(monkeypatch, tmp_path):
    monkeypatch.setenv("API_JWT_SEGREDO", "x")
    monkeypatch.setenv("DB_NAME", str(tmp_path / "cep_xray.fdb"))
    cfg = ConfigAPI.do_ambiente()
    assert cfg.firebird is not None
    assert cfg.firebird.database.endswith("cep_xray.fdb")


# A regressão de verdade, num processo limpo -------------------------------

SEM_DOTENV = """
import os, sys
os.chdir(sys.argv[1])
# Nada de `load_dotenv`, e o ambiente foi esvaziado: é a situação de qualquer
# script que chame `criar_app` sem passar pelo entrypoint.
from src.api.config import ConfigAPI
try:
    ConfigAPI.do_ambiente()
except RuntimeError as e:
    print("RECUSOU:", "DB_NAME" in str(e))
else:
    print("ACEITOU")
"""


def test_sem_ambiente_carregado_a_configuracao_recusa_nascer():
    """Sem `.env` e sem `ENV`, falha na CONSTRUÇÃO — não na primeira consulta.

    Processo separado e ambiente podado de propósito: aqui dentro o pytest já
    importou meio mundo, e um `src.consulta.cep` importado por outro teste
    carregaria o `.env` e mascararia exatamente o que se quer provar.
    """
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONPATH")}
    env["API_JWT_SEGREDO"] = "x"
    env["PYTHONPATH"] = str(RAIZ)

    r = subprocess.run(
        [sys.executable, "-c", SEM_DOTENV, str(RAIZ)],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "RECUSOU: True", r.stdout
