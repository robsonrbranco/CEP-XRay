"""Configuração de acesso ao Firebird, lida do ambiente (.env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Firebird 3.0 aceita no máximo 16 KB de página (32 KB só a partir do 4.0).
# Página grande reduz profundidade de índice e melhora leitura sequencial, que
# é o perfil desta base.
#
# Medido em 2026-09-17, mesmas 1.655.969 linhas reais em cada braço, com
# USE_FULL e os índices do projeto (o script está descrito no PR):
#
#     page_size   dados    índices   total     vs menor
#      4.096    172,9 MB   19,0 MB   191,9 MB   +0,8%
#      8.192    171,0 MB   19,4 MB   190,5 MB      —
#     16.384    170,9 MB   19,8 MB   190,7 MB   +0,1%
#
# Ou seja: **page_size praticamente não afeta o tamanho final**. Os dois
# componentes andam em direções opostas e quase se cancelam — página pequena
# multiplica o cabeçalho fixo de cada página de dado, e página grande desperdiça
# mais no resto de cada nó de índice parcialmente cheio. A diferença entre o
# melhor e o pior é 1,4 MB em 190 MB; nos 33 GB da base seriam ~33 MB entre
# 16 KB e 8 KB, e 4 KB (a escolha intuitiva de quem quer arquivo menor) é a
# PIOR das três.
#
# Como espaço é indiferente, o critério de escolha volta a ser profundidade de
# índice e leitura sequencial — e aí 16 KB ganha. Não trocar sem uma razão nova:
# page_size só pode ser definido na criação, mudar exige backup/restore com gbak.
DEFAULT_PAGE_SIZE = 16384

# Quanto maior o batch, menos idas ao servidor — mas cada batch vira uma lista
# de tuplas viva em memória. 50k linhas de estabelecimento ~ 100 MB.
DEFAULT_BATCH_SIZE = 50_000

# Linhas por transação. Transação longa demais em Firebird segura a versão
# antiga das páginas e infla o banco; curta demais paga commit à toa.
DEFAULT_COMMIT_EVERY = 500_000


def _env(name: str, default=None):
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


def _total_ram_mb() -> int:
    """RAM física total, para dimensionar o cache. 0 se não der para descobrir."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys // (1024 * 1024))
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except Exception:
        return 0


@dataclass
class FirebirdConfig:
    host: str = "localhost"
    port: int = 3050
    # Caminho do .fdb COMO O SERVIDOR o enxerga.
    database: str = ""
    user: str = "SYSDBA"
    password: str = ""
    charset: str = "WIN1252"
    collation: str = "WIN_PTBR"
    page_size: int = DEFAULT_PAGE_SIZE
    # Tamanho do cache de páginas do banco, em MB. 0 = deixa o padrão do servidor.
    cache_mb: int = 0
    batch_size: int = DEFAULT_BATCH_SIZE
    commit_every: int = DEFAULT_COMMIT_EVERY
    # Biblioteca cliente (fbclient.dll / libfbclient.so). Vazio = busca no PATH.
    client_library: str = ""
    # Diretório onde ESTE processo enxerga os .fdb, quando difere do caminho
    # que o servidor usa (Firebird em container ou noutra máquina). Só importa
    # para a troca blue-green, que renomeia arquivos. Ver caminho_local().
    local_data_dir: str = ""
    # Modo embedded: o engine roda DENTRO deste processo, sem servidor.
    #
    # Muda o DSN, e essa é a parte que morde: embedded exige caminho SEM host.
    # Com `inet://host:porta/caminho` o Dispatcher tenta o provider Remote,
    # não encontra servidor e devolve "unavailable database" -- erro que não
    # diz que o problema foi a forma do caminho.
    #
    # É o modo do pod de consulta. O ETL continua falando com o servidor,
    # porque carga em massa quer cache compartilhado e Force Write ajustável.
    embedded: bool = False

    @property
    def dsn(self) -> str:
        """DSN no formato aceito pelo firebird-driver."""
        if self.embedded:
            return self.database
        return f"inet://{self.host}:{self.port}/{self.database}"

    def dsn_para(self, caminho: str) -> str:
        """DSN de outro arquivo de banco no mesmo servidor."""
        if self.embedded:
            return caminho
        return f"inet://{self.host}:{self.port}/{caminho}"

    def _irmao(self, sufixo: str) -> str:
        """Caminho de um .fdb irmão do ativo, com sufixo no nome."""
        p = PurePosixPath(self.database)
        return str(p.with_name(f"{p.stem}{sufixo}{p.suffix}"))

    @property
    def staging_database(self) -> str:
        """Banco onde o ETL constrói a base nova, do zero."""
        return self._irmao("_staging")

    @property
    def old_database(self) -> str:
        """Banco anterior, mantido só entre o rename e o descarte."""
        return self._irmao("_old")

    def caminho_local(self, caminho_servidor: str) -> Path:
        """Traduz um caminho do servidor para onde ESTE processo o enxerga.

        A troca blue-green renomeia arquivos, e quem renomeia é o processo
        Python — não o servidor. Quando o Firebird roda em container ou em
        outra máquina, o caminho que o servidor usa não é o mesmo que o
        cliente vê; `FB_LOCAL_DATA_DIR` faz essa ponte. Vazio significa que
        cliente e servidor enxergam o mesmo caminho.
        """
        nome = PurePosixPath(caminho_servidor).name
        if self.local_data_dir:
            return Path(self.local_data_dir) / nome
        return Path(caminho_servidor)

    @property
    def cache_pages(self) -> int:
        """Cache em páginas, que é a unidade que o Firebird usa."""
        if self.cache_mb <= 0:
            return 0
        return max(1024, (self.cache_mb * 1024 * 1024) // self.page_size)

    def describe(self) -> str:
        cache = (
            f"{self.cache_mb} MB ({self.cache_pages:,} páginas)"
            if self.cache_mb > 0
            else "padrão do servidor"
        )
        return (
            f"servidor .... {self.host}:{self.port}\n"
            f"banco ....... {self.database}\n"
            f"usuário ..... {self.user}\n"
            f"charset ..... {self.charset}\n"
            f"page size ... {self.page_size} bytes\n"
            f"cache ....... {cache}\n"
            f"batch ....... {self.batch_size:,} linhas\n"
            f"commit ...... a cada {self.commit_every:,} linhas"
        )


def load_config() -> FirebirdConfig:
    """Monta a configuração a partir das variáveis de ambiente."""
    database = _env("DB_NAME", "")
    if database:
        # Normaliza separadores: o servidor Windows aceita ambos, mas o DSN
        # fica ilegível com barra invertida misturada.
        database = str(Path(database)).replace("\\", "/")

    cache_mb = _int_env("FB_CACHE_MB", 0)
    if cache_mb < 0:
        # Negativo = fração da RAM física. -50 significa "metade da RAM".
        ram = _total_ram_mb()
        cache_mb = int(ram * min(abs(cache_mb), 90) / 100) if ram else 0

    return FirebirdConfig(
        host=_env("DB_HOST", "localhost"),
        port=_int_env("DB_PORT", 3050),
        database=database,
        user=_env("DB_USER", "SYSDBA"),
        password=_env("DB_PASSWORD", ""),
        embedded=_env("FB_EMBEDDED", "").lower() in ("1", "true", "sim"),
        charset=_env("DB_CHARSET", "WIN1252"),
        collation=_env("DB_COLLATION", "WIN_PTBR"),
        page_size=_int_env("FB_PAGE_SIZE", DEFAULT_PAGE_SIZE),
        cache_mb=cache_mb,
        batch_size=_int_env("FB_BATCH_SIZE", DEFAULT_BATCH_SIZE),
        commit_every=_int_env("FB_COMMIT_EVERY", DEFAULT_COMMIT_EVERY),
        client_library=_env("FB_CLIENT_LIBRARY", ""),
        local_data_dir=_env("FB_LOCAL_DATA_DIR", ""),
    )
