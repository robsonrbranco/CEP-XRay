"""Testes da troca blue-green.

Este é o código que pode destruir a base de produção, e o caminho de erro dele
nunca tinha sido executado — nem em teste nem na prática, porque as trocas
reais deram certo. `_restaurar()` era uma suposição escrita em Python.

A troca é renomeação de arquivo, então dá para testá-la honestamente sem
Firebird nenhum: arquivos de verdade num diretório temporário, com conteúdo
distinto para provar QUAL arquivo acabou em cada lugar. O que precisa de
servidor — `desligar`/`religar`, que só tiram o banco de uso — vira no-op, e
isso não enfraquece o teste: eles não movem nem renomeiam nada.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.blue_green import switch as mod_switch  # noqa: E402
from src.blue_green.state import StateManager  # noqa: E402
from src.db.config import FirebirdConfig  # noqa: E402


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Diretório com os três caminhos da troca e um switcher pronto."""
    monkeypatch.setattr(mod_switch.manutencao, "desligar", lambda *a, **k: None)
    monkeypatch.setattr(mod_switch.manutencao, "religar", lambda *a, **k: None)

    cfg = FirebirdConfig(database=str(tmp_path / "cnpj.fdb").replace("\\", "/"))
    state = StateManager(str(tmp_path / "state.json"))
    switcher = mod_switch.BlueGreenSwitcher(cfg=cfg, state=state)

    return {
        "switcher": switcher,
        "cfg": cfg,
        "ativo": tmp_path / "cnpj.fdb",
        "staging": tmp_path / "cnpj_staging.fdb",
        "old": tmp_path / "cnpj_old.fdb",
    }


def test_troca_coloca_a_base_nova_em_producao(ambiente):
    ambiente["ativo"].write_text("BASE ANTIGA")
    ambiente["staging"].write_text("BASE NOVA")

    r = ambiente["switcher"].switch(force=True)

    assert r.success, r.message
    # O conteúdo prova qual arquivo ficou onde — checar existência não provaria.
    assert ambiente["ativo"].read_text() == "BASE NOVA"
    assert not ambiente["staging"].exists()
    assert not ambiente["old"].exists(), "a base anterior deveria ter sido descartada"


def test_primeira_carga_sem_base_anterior(ambiente):
    """Não existe produção ainda: a nova entra e nada precisa ser preservado."""
    ambiente["staging"].write_text("BASE NOVA")

    r = ambiente["switcher"].switch(force=True)

    assert r.success, r.message
    assert ambiente["ativo"].read_text() == "BASE NOVA"
    assert not ambiente["old"].exists()


def test_sem_base_nova_nao_toca_na_producao(ambiente):
    ambiente["ativo"].write_text("BASE ANTIGA")

    r = ambiente["switcher"].switch(force=True)

    assert not r.success
    assert "não encontrada" in r.message
    assert ambiente["ativo"].read_text() == "BASE ANTIGA", "produção foi mexida sem base nova"


def test_sobra_de_troca_interrompida_e_descartada(ambiente):
    """Um `_old` remanescente faria o rename do ativo esbarrar em arquivo existente."""
    ambiente["ativo"].write_text("BASE ANTIGA")
    ambiente["staging"].write_text("BASE NOVA")
    ambiente["old"].write_text("SOBRA DE UMA TROCA ANTERIOR")

    r = ambiente["switcher"].switch(force=True)

    assert r.success, r.message
    assert ambiente["ativo"].read_text() == "BASE NOVA"
    assert not ambiente["old"].exists()


def test_falha_no_segundo_rename_restaura_a_base_anterior(ambiente, monkeypatch):
    """O caso que justifica o `_old` existir.

    A produção já foi renomeada para `_old` e o rename do staging falha. Sem
    restauração, não sobraria base de produção nenhuma em disco — é o pior
    desfecho possível deste módulo.
    """
    ambiente["ativo"].write_text("BASE ANTIGA")
    ambiente["staging"].write_text("BASE NOVA")

    rename_real = Path.rename

    def rename_que_falha_ao_promover(self, destino):
        if Path(destino).name == "cnpj.fdb" and self.name == "cnpj_staging.fdb":
            raise OSError("disco cheio")
        return rename_real(self, destino)

    monkeypatch.setattr(Path, "rename", rename_que_falha_ao_promover)

    r = ambiente["switcher"].switch(force=True)

    assert not r.success
    assert "restaurada" in r.message, r.message
    assert ambiente["ativo"].exists(), "ficou sem base de produção em disco"
    assert ambiente["ativo"].read_text() == "BASE ANTIGA"
    assert ambiente["staging"].read_text() == "BASE NOVA", "a base nova foi perdida"


def test_falha_no_primeiro_rename_deixa_producao_no_lugar(ambiente, monkeypatch):
    """Falhar antes de mexer na produção tem que ser inofensivo."""
    ambiente["ativo"].write_text("BASE ANTIGA")
    ambiente["staging"].write_text("BASE NOVA")

    rename_real = Path.rename

    def rename_que_falha_ao_preservar(self, destino):
        if Path(destino).name == "cnpj_old.fdb":
            raise OSError("permissão negada")
        return rename_real(self, destino)

    monkeypatch.setattr(Path, "rename", rename_que_falha_ao_preservar)

    r = ambiente["switcher"].switch(force=True)

    assert not r.success
    assert ambiente["ativo"].read_text() == "BASE ANTIGA"
    assert ambiente["staging"].read_text() == "BASE NOVA"


def test_falha_ao_descartar_a_antiga_nao_invalida_a_troca(ambiente, monkeypatch):
    """A troca já terminou; não conseguir apagar o `_old` é sujeira, não erro.

    Inverter isso seria pior: reportar falha faria o operador desfazer uma
    troca que na verdade deu certo.
    """
    ambiente["ativo"].write_text("BASE ANTIGA")
    ambiente["staging"].write_text("BASE NOVA")

    unlink_real = Path.unlink

    def unlink_que_falha(self, *a, **k):
        if self.name == "cnpj_old.fdb":
            raise OSError("arquivo em uso")
        return unlink_real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink_que_falha)

    r = ambiente["switcher"].switch(force=True)

    assert r.success, r.message
    assert ambiente["ativo"].read_text() == "BASE NOVA"
    assert ambiente["old"].read_text() == "BASE ANTIGA", "o _old deveria ter sobrado"


def test_validacao_reprovada_bloqueia_a_troca(ambiente, monkeypatch):
    """Sem `force`, base nova reprovada não entra em produção."""
    ambiente["ativo"].write_text("BASE ANTIGA")
    ambiente["staging"].write_text("BASE NOVA MAS RUIM")

    monkeypatch.setattr(
        mod_switch, "validar",
        lambda cfg, db: mod_switch.ValidationResult(
            is_valid=False, empty_tables=["estabelecimento"]
        ),
    )

    r = ambiente["switcher"].switch()

    assert not r.success
    assert "tabelas vazias: estabelecimento" in r.message
    assert ambiente["ativo"].read_text() == "BASE ANTIGA"
    assert ambiente["staging"].exists()


def test_descartar_antiga_remove_a_sobra(ambiente):
    ambiente["old"].write_text("SOBRA")

    msg = ambiente["switcher"].descartar_antiga()

    assert "removida" in msg
    assert not ambiente["old"].exists()


def test_descartar_antiga_sem_sobra_nao_faz_nada(ambiente):
    msg = ambiente["switcher"].descartar_antiga()
    assert "não existe" in msg
