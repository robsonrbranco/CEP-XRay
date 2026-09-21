"""Conexão viva com a base, por worker.

Por que existe
--------------
Em Firebird embedded o attachment não é uma conexão de rede barata: o engine
inicializa dentro do processo e lê páginas de cabeçalho e metadados. Medido
contra a base real de 34,2 GB, em container:

    abrir um attachment ....................  387 ms
    a consulta indexada, já conectado ......    0,4 ms
    ficha completa, conexão por requisição .  772 ms
    ficha completa, conexão viva ...........   11,6 ms   (66x)

Ou seja, a API estava pagando 387 ms por requisição para nada.

Por que segurar conexão aberta é seguro AQUI
--------------------------------------------
Os dois motivos usuais para não fazer isso não se aplicam:

* **Invalidação** — a base é read-only e imutável dentro da vida do pod. Não há
  escrita de outro processo para enxergar, e a troca mensal derruba o pod.
* **Transação longa** — em base que não muda não há versão antiga de página a
  segurar nem lixo a acumular.

O que sobra é conexão morrer, e disso este módulo trata.
"""

from __future__ import annotations

import logging
import threading

from ..db import connection

logger = logging.getLogger(__name__)


class ConexaoViva:
    """Uma conexão por processo, reaberta se cair.

    O `threading.Lock` existe porque a suposição de acesso serial é frágil: hoje
    as rotas são `async def` e rodam no laço de eventos, uma de cada vez, mas
    trocar uma delas para `def` faz o FastAPI executá-la num pool de threads --
    e `Connection` do firebird-driver não é thread-safe. O lock custa
    microssegundos e remove a armadilha.
    """

    def __init__(self, cfg=None):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._con = None

    def _abrir(self):
        ctx = connection.conectar(self._cfg)
        con = ctx.__enter__()
        self._ctx = ctx
        return con

    def _fechar(self):
        if self._con is not None:
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:                               # noqa: BLE001
                pass
            self._con = None

    def executar(self, funcao):
        """Roda `funcao(con)` com a conexão viva, reabrindo uma vez se ela caiu.

        A reabertura acontece **uma** vez. Insistir mais esconderia uma base
        que sumiu do volume atrás de latência crescente, em vez de deixar o erro
        aparecer.
        """
        with self._lock:
            if self._con is None:
                self._con = self._abrir()
            try:
                return funcao(self._con)
            except Exception as e:                          # noqa: BLE001
                logger.warning("conexão caiu (%s) — reabrindo", e)
                self._fechar()
                self._con = self._abrir()
                return funcao(self._con)

    def fechar(self):
        with self._lock:
            self._fechar()
