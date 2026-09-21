"""O Hugo do estágio `site` é uma versão fixa, com checksum.

Antes, o `Dockerfile` descobria a versão em tempo de build com uma chamada
**não autenticada** a `api.github.com`. Isso reprovou o deploy de 2026-09-21:
a resposta veio sem `tag_name` (limite por IP do runner), o `grep -oP` saiu 1 e
a cadeia `&&` parou — 40 minutos depois de a mesma imagem ter sido construída
com sucesso.

A falha intermitente era o sintoma menor. O maior é que dois builds do MESMO
commit podiam produzir imagens com Hugos diferentes, sem que nada no diff
dissesse isso.

Estes testes são de TEXTO, e é o que dá para fazer sem Docker nesta suíte: eles
não constroem a imagem, apenas impedem que a propriedade volte a se perder em
silêncio.
"""

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DOCKERFILE = (RAIZ / "Dockerfile").read_text(encoding="utf-8")


def test_a_versao_do_hugo_esta_fixada():
    assert re.search(r"^ARG HUGO_VERSION=\d+\.\d+\.\d+$", DOCKERFILE, re.M), (
        "ARG HUGO_VERSION sumiu ou deixou de ser uma versão literal"
    )


def test_o_build_nao_consulta_a_api_do_github():
    """A regressão de verdade: qualquer volta ao 'último' reprova aqui.

    O texto está fora de comentário porque o comentário ACIMA do ARG cita a
    falha e menciona o GitHub de propósito — travar a palavra solta faria este
    teste brigar com a própria documentação.
    """
    codigo = "\n".join(
        l for l in DOCKERFILE.splitlines() if not l.lstrip().startswith("#")
    )
    assert "api.github.com" not in codigo, (
        "o Dockerfile voltou a descobrir a versão em tempo de build — "
        "é a falha por limite de requisições, e o build deixa de ser reprodutível"
    )


def test_o_download_e_conferido_por_checksum():
    assert re.search(r"^ARG HUGO_SHA256=[0-9a-f]{64}$", DOCKERFILE, re.M), (
        "ARG HUGO_SHA256 ausente ou não é um sha256 de 64 hexadecimais"
    )
    assert "sha256sum -c" in DOCKERFILE, (
        "o checksum está declarado mas ninguém o confere — pior que não ter, "
        "porque parece garantia"
    )


def test_o_checksum_vem_antes_de_desempacotar():
    """Conferir depois do `tar` não protege de nada."""
    conferencia = DOCKERFILE.index("sha256sum -c")
    desempacota = DOCKERFILE.index("tar -xzf /tmp/hugo.tar.gz")
    assert conferencia < desempacota
