"""Servidor do `tc web` (T24): HTTP local, só leitura, só em 127.0.0.1 (regra 2 do AGENTS.md).

- Escuta só na interface de loopback; nunca em 0.0.0.0 nem no IP da máquina. Não abre nenhuma conexão de saída.
- Só `GET`; outros métodos recebem 405. O cabeçalho `Host` precisa ser o do próprio loopback (contra DNS rebinding).
- As requisições leem o SQLite numa conexão `mode=ro`: uma escrita por ela falha.
- Uma thread em segundo plano roda o ingest incremental a cada `intervalo` segundos, com a própria conexão de escrita
  (o banco fica em WAL para o ingest não travar as leituras). Nunca roda duas vezes ao mesmo tempo.
- Nada de log de requisição (as URLs têm IDs de sessão) e nenhum recurso externo nas páginas (CSP `default-src 'none'`).
"""

from __future__ import annotations

import errno
import json
import socket
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import consultas, db, demo, diagnostico
from ..fontes import claude_code
from . import telas

ENDERECO = "127.0.0.1"           # único endereço permitido (regra 2)
PORTA_PADRAO = 8765
TENTATIVAS_DE_PORTA = 20
INTERVALO_S = 5
MAX_PARAMETRO = 200              # tamanho máximo do valor de periodo/projeto vindo da URL
MAX_PARAMETROS = 10              # a URL com mais parâmetros que isso é lida como se não tivesse nenhum
CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


# ---------- banco ----------

def conectar_leitura(caminho: Path) -> sqlite3.Connection:
    """Conexão só leitura (`mode=ro`): qualquer INSERT/UPDATE/DELETE por ela levanta `sqlite3.OperationalError`."""
    con = sqlite3.connect(f"{Path(caminho).resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    return con


class Ingestor(threading.Thread):
    """Roda o ingest incremental a cada `intervalo` segundos, com uma conexão de escrita só dela."""

    def __init__(self, caminho: Path, intervalo: float, apelidos: dict | None = None):
        super().__init__(name="tc-web-ingest", daemon=True)
        self.caminho, self.intervalo, self.apelidos = caminho, intervalo, apelidos or {}
        self.parar = threading.Event()
        self.pronto = threading.Event()   # a conexão de escrita está aberta (os arquivos -wal/-shm existem)
        self.trava = threading.Lock()     # nunca duas execuções ao mesmo tempo
        self.ultimo_erro: str | None = None
        self.rodadas = 0

    def rodar_uma_vez(self, con: sqlite3.Connection) -> bool:
        """Uma rodada; devolve False (sem esperar) se outra ainda estiver em andamento."""
        if not self.trava.acquire(blocking=False):
            return False
        try:
            claude_code.ingerir(con, apelidos=self.apelidos)
            self.ultimo_erro = None
            self.rodadas += 1
        except sqlite3.Error as e:      # banco ocupado por outro `tc` (ex.: tc live): tenta de novo na próxima rodada
            self.ultimo_erro = type(e).__name__
            con.rollback()
        finally:
            self.trava.release()
        return True

    def run(self) -> None:
        con = db.conectar(self.caminho)
        con.execute("PRAGMA busy_timeout = 5000")
        self.pronto.set()
        try:
            while not self.parar.is_set():
                self.rodar_uma_vez(con)
                self.parar.wait(self.intervalo)
        finally:
            con.close()


def preparar_banco(caminho: Path) -> None:
    """Cria/migra o banco e liga o WAL (persistente no arquivo), antes de o servidor abrir conexões só leitura."""
    con = db.conectar(caminho)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    finally:
        con.close()


# ---------- URL ----------

def opcoes_da_url(query: str) -> tuple[str, str | None, list[str]]:
    """(período, projeto, avisos) da query de `/`. Nunca levanta: o que for inválido é ignorado e vira um aviso.

    Só `periodo` e `projeto` valem; outros parâmetros são ignorados. Parâmetro repetido vale o primeiro. Período fora da
    lista volta para "hoje". Projeto só tem o espaço das pontas removido e o tamanho limitado (o filtro é uma igualdade
    exata no SQL, com parâmetro; nunca vira texto de consulta).
    """
    avisos: list[str] = []
    try:
        q = parse_qs(query, keep_blank_values=False, max_num_fields=MAX_PARAMETROS, errors="replace")
    except ValueError:
        q = {}
        avisos.append("A URL tem parâmetros demais: eles foram ignorados.")
    periodo = (q.get("periodo") or ["hoje"])[0].strip()[:MAX_PARAMETRO]
    if periodo not in consultas.PERIODOS:
        avisos.append(f"O período “{periodo[:40]}” não existe; mostrando Hoje. Valores aceitos: "
                      + ", ".join(consultas.PERIODOS) + ".")
        periodo = "hoje"
    projeto = ((q.get("projeto") or [""])[0].strip()[:MAX_PARAMETRO]) or None
    return periodo, projeto, avisos


# ---------- HTTP ----------

class Servidor(ThreadingHTTPServer):
    """ThreadingHTTPServer que NÃO reaproveita porta ocupada (no Windows, SO_REUSEADDR deixaria dois servidores na mesma)."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: ninguém mais escuta nesta porta enquanto estivermos nela
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, porta: int, caminho: Path, limites: dict, demo_: bool, intervalo: int):
        super().__init__((ENDERECO, porta), Pagina)
        self.caminho, self.limites, self.demo, self.intervalo = caminho, limites, demo_, intervalo
        self.porta = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.porta}", f"localhost:{self.porta}"}

    def agora(self) -> datetime:
        return demo.agora() if self.demo else datetime.now(timezone.utc)


class Pagina(BaseHTTPRequestHandler):
    server: Servidor
    server_version = "tc-web"
    sys_version = ""

    def log_message(self, formato, *args) -> None:  # noqa: D401 - sem log: as URLs têm IDs de sessão
        return

    def _responder(self, status: int, corpo: str, tipo: str = "text/html; charset=utf-8", extra: dict | None = None):
        dados = corpo.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(dados)

    def _nao_permitido(self) -> None:
        self._responder(HTTPStatus.METHOD_NOT_ALLOWED, "só GET", "text/plain; charset=utf-8", {"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = _nao_permitido

    def do_GET(self) -> None:
        if (self.headers.get("Host") or "").lower() not in self.server.hosts:
            self._responder(HTTPStatus.FORBIDDEN, "Host não permitido: use http://127.0.0.1:%d/" % self.server.porta,
                            "text/plain; charset=utf-8")
            return
        partes = urlsplit(self.path)
        caminho = unquote(partes.path)
        try:
            if caminho == "/":
                self._responder(HTTPStatus.OK, self._visao(partes.query))
            elif caminho == "/estilo.css":
                self._responder(HTTPStatus.OK, telas.estilo_css(), "text/css; charset=utf-8")
            elif caminho.startswith("/sessao/"):
                self._responder(HTTPStatus.OK, telas.recado(
                    "Detalhe da sessão", "Esta tela chega na próxima etapa (T26). Por enquanto, rode "
                    f"“tc sessao {caminho[len('/sessao/'):][:8]}” no terminal."))
            else:
                self._responder(HTTPStatus.NOT_FOUND, telas.recado("Página não encontrada", "Esse endereço não existe no tc web."))
        except sqlite3.Error as e:
            self._responder(HTTPStatus.SERVICE_UNAVAILABLE, telas.recado(
                "Banco indisponível", f"Não foi possível ler o banco agora ({type(e).__name__}). A página tenta de novo."))
        except Exception as e:  # noqa: BLE001 - nunca derruba o servidor; o detalhe não vai para a página nem para log
            self._responder(HTTPStatus.INTERNAL_SERVER_ERROR, telas.recado(
                "Erro ao montar a página", f"Falha interna ({type(e).__name__}). Rode `python -m pytest` para investigar."))

    def _visao(self, query: str) -> str:
        s = self.server
        periodo, projeto, avisos = opcoes_da_url(query)
        momento = s.agora()
        con = conectar_leitura(s.caminho)
        try:
            d = consultas.visao(con, periodo, momento.astimezone().date(), momento, s.limites, projeto)
        finally:
            con.close()
        return telas.visao(d, s.limites, s.demo, s.intervalo, f"{ENDERECO}:{s.porta}", avisos)


# ---------- subir ----------

def porta_configurada(caminho_db: Path) -> int:
    """`web_porta` do config.json ao lado do banco; senão a padrão."""
    arq = Path(caminho_db).parent / "config.json"
    try:
        v = json.loads(arq.read_text(encoding="utf-8")).get("web_porta")
    except (OSError, ValueError, AttributeError):
        return PORTA_PADRAO
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535 else PORTA_PADRAO


def criar_servidor(porta: int, caminho: Path, limites: dict, demo_: bool = False, intervalo: int = INTERVALO_S,
                   tentativas: int = TENTATIVAS_DE_PORTA) -> Servidor:
    """Liga na `porta` ou, se ocupada, na próxima livre (até `tentativas`). Porta 0 = o sistema escolhe."""
    ultimo: OSError | None = None
    for p in ([0] if porta == 0 else range(porta, min(65536, porta + tentativas))):
        try:
            return Servidor(p, caminho, limites, demo_, intervalo)
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), 10013, 10048):
                raise
            ultimo = e
    raise OSError(f"nenhuma porta livre entre {porta} e {porta + tentativas - 1}") from ultimo


def executar(caminho_db: Path | None, porta: int | None, demo_: bool, limites: dict | None = None,
             apelidos: dict | None = None) -> None:
    """`tc web`: sobe o servidor e (fora do modo demo) a thread de ingest. Ctrl+C encerra."""
    caminho = Path(caminho_db or db.caminho_padrao())
    ingestor = None
    if demo_:
        caminho = demo.gerar(Path(tempfile.mkdtemp(prefix="tc-web-demo-")) / "demo.db")
        limites = diagnostico.carregar_limites(None)[0]
    else:
        preparar_banco(caminho)
        ingestor = Ingestor(caminho, INTERVALO_S, apelidos)
        ingestor.start()
        ingestor.pronto.wait(10)
    porta = porta_configurada(caminho) if porta is None else porta
    servidor = criar_servidor(porta, caminho, limites or diagnostico.carregar_limites(None)[0], demo_)
    url = f"http://{ENDERECO}:{servidor.porta}/"
    if servidor.porta != porta and porta:
        print(f"A porta {porta} está ocupada; usando {servidor.porta}.")
    print(f"tc web{' (demonstração, dados fictícios)' if demo_ else ''}: {url}  (Ctrl+C para sair)")
    sys.stdout.flush()
    try:
        servidor.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
    finally:
        servidor.server_close()
        if ingestor:
            ingestor.parar.set()
            ingestor.join(5)
