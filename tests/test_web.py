"""tc web (T24): servidor só em 127.0.0.1, só GET, Host checado, banco só leitura, escape e a tela Hoje.

Os testes só falam com 127.0.0.1 (nenhuma rede externa) e usam o banco de demonstração ou bancos sintéticos.
"""

from __future__ import annotations

import http.client
import re
import socket
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from token_controler import consultas, db, demo, diagnostico
from token_controler.web import servidor, telas

LIM = diagnostico.carregar_limites(None)[0]


# ---------- servidor de teste ----------

@pytest.fixture(scope="module")
def banco_demo(tmp_path_factory) -> Path:
    return demo.gerar(tmp_path_factory.mktemp("demo") / "demo.db")


@pytest.fixture
def no_ar(banco_demo):
    """Servidor --demo numa porta livre escolhida pelo sistema; desliga no fim."""
    srv = servidor.criar_servidor(0, banco_demo, LIM, demo_=True)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def pedir(srv, metodo="GET", caminho="/", host=None):
    con = http.client.HTTPConnection("127.0.0.1", srv.porta, timeout=10)
    con.putrequest(metodo, caminho, skip_host=True)
    con.putheader("Host", host if host is not None else f"127.0.0.1:{srv.porta}")
    con.endheaders()
    r = con.getresponse()
    corpo = r.read().decode("utf-8")
    con.close()
    return r, corpo


# ---------- segurança ----------

def test_escuta_so_no_loopback(no_ar):
    assert servidor.ENDERECO == "127.0.0.1"
    assert no_ar.server_address[0] == "127.0.0.1"
    assert no_ar.socket.getsockname()[0] == "127.0.0.1"


def test_so_aceita_get(no_ar):
    for metodo in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        r, _ = pedir(no_ar, metodo)
        assert r.status == 405, metodo
        assert r.getheader("Allow") == "GET"


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{p}", "0.0.0.0:{p}", "192.168.0.10:{p}", ""])
def test_host_que_nao_e_loopback_e_recusado(no_ar, host):
    r, corpo = pedir(no_ar, host=host.format(p=no_ar.porta))
    assert r.status == 403 and "tc web" not in corpo


@pytest.mark.parametrize("host", ["127.0.0.1:{p}", "localhost:{p}", "LOCALHOST:{p}"])
def test_host_de_loopback_e_aceito(no_ar, host):
    r, _ = pedir(no_ar, host=host.format(p=no_ar.porta))
    assert r.status == 200


def test_cabecalhos_de_seguranca_e_sem_script(no_ar):
    r, corpo = pedir(no_ar)
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "script-src" not in csp
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert "<script" not in corpo.lower()
    # nenhum recurso externo: todo href/src é relativo ao próprio servidor ou data:
    for url in re.findall(r'(?:href|src)="([^"]*)"', corpo):
        assert url.startswith(("/", "data:")), url


def test_banco_aberto_so_para_leitura(banco_demo):
    con = servidor.conectar_leitura(banco_demo)
    try:
        assert con.execute("SELECT COUNT(*) FROM chamadas").fetchone()[0] > 0
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM chamadas")
    finally:
        con.close()


def test_leitura_mode_ro_num_banco_em_wal(tmp_path):
    """O banco real fica em WAL; com a conexão de escrita aberta (a do ingest), a leitura mode=ro funciona."""
    caminho = tmp_path / "wal.db"
    servidor.preparar_banco(caminho)
    escrita = db.conectar(caminho)
    try:
        assert escrita.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        escrita.execute("INSERT INTO chamadas (id, ferramenta, sessao_id, ts, arquivo) VALUES ('a', 'x', 's', 't', 'f')")
        escrita.commit()
        leitura = servidor.conectar_leitura(caminho)
        assert leitura.execute("SELECT COUNT(*) FROM chamadas").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            leitura.execute("INSERT INTO chamadas (id, ferramenta, sessao_id, ts, arquivo) VALUES ('b','x','s','t','f')")
        leitura.close()
    finally:
        escrita.close()


def test_porta_ocupada_vai_para_a_proxima(banco_demo):
    ocupante = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ocupante.bind(("127.0.0.1", 0))
    ocupante.listen(1)
    porta = ocupante.getsockname()[1]
    try:
        srv = servidor.criar_servidor(porta, banco_demo, LIM, demo_=True)
        try:
            assert srv.porta != porta and porta < srv.porta <= porta + servidor.TENTATIVAS_DE_PORTA
        finally:
            srv.server_close()
    finally:
        ocupante.close()


def test_porta_do_config(tmp_path):
    (tmp_path / "config.json").write_text('{"web_porta": 9123}', encoding="utf-8")
    assert servidor.porta_configurada(tmp_path / "tracker.db") == 9123
    (tmp_path / "config.json").write_text('{"web_porta": "x"}', encoding="utf-8")
    assert servidor.porta_configurada(tmp_path / "tracker.db") == servidor.PORTA_PADRAO


def test_ingest_nunca_roda_duas_vezes_ao_mesmo_tempo(tmp_path):
    ing = servidor.Ingestor(tmp_path / "x.db", 5)
    ing.trava.acquire()
    try:
        assert ing.rodar_uma_vez(None) is False     # outra rodada em andamento: sai sem tocar no banco
    finally:
        ing.trava.release()


def test_thread_de_ingest_le_os_logs(tmp_path):
    pasta = tmp_path / "projects" / "p"
    pasta.mkdir(parents=True)
    (pasta / "s.jsonl").write_text(
        '{"type":"assistant","timestamp":"2026-11-22T10:00:00.000Z","sessionId":"s1","requestId":"r1",'
        '"message":{"id":"m1","model":"claude-sonnet-5-5","usage":{"input_tokens":1,"output_tokens":2}}}\n',
        encoding="utf-8")
    caminho = tmp_path / "t.db"
    servidor.preparar_banco(caminho)
    ing = servidor.Ingestor(caminho, 5)
    con = db.conectar(caminho)
    try:
        from token_controler.fontes import claude_code
        original = claude_code.pasta_padrao
        claude_code.pasta_padrao = lambda: tmp_path / "projects"
        try:
            assert ing.rodar_uma_vez(con) is True
        finally:
            claude_code.pasta_padrao = original
        assert con.execute("SELECT COUNT(*) FROM chamadas").fetchone()[0] == 1 and ing.rodadas == 1
    finally:
        con.close()


# ---------- rotas ----------

def test_hoje_responde_200(no_ar):
    r, corpo = pedir(no_ar)
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert "Sessões de hoje" in corpo and "analisador-curriculos" in corpo


def test_demo_sem_recarregar_e_deterministico(no_ar):
    _, a = pedir(no_ar)
    _, b = pedir(no_ar)
    assert a == b
    assert 'http-equiv="refresh"' not in a and "Demonstração" in a and "11:07:42" in a


def test_estilo_e_rotas_desconhecidas(no_ar):
    r, css = pedir(no_ar, caminho="/estilo.css")
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/css") and ".kpi" in css
    r, _ = pedir(no_ar, caminho="/nada")
    assert r.status == 404
    r, corpo = pedir(no_ar, caminho="/sessao/demo0002")
    assert r.status == 200 and "T26" in corpo
    r, corpo = pedir(no_ar, caminho="/sessao/%3Cb%3E")
    assert "<b>" not in corpo and "&lt;b&gt;" in corpo


# ---------- tela Hoje: números e escape ----------

def _kpis(corpo: str) -> dict[str, str]:
    return dict(re.findall(r'data-kpi="(\w+)">([^<]*)<', corpo))


def test_numeros_da_pagina_batem_com_consultas(no_ar, banco_demo):
    _, corpo = pedir(no_ar)
    con = servidor.conectar_leitura(banco_demo)
    try:
        d = consultas.dia(con, date(*demo.DIA), demo.agora(), LIM)
    finally:
        con.close()
    k = _kpis(corpo)
    assert k["custo"] == telas.usd(d["custo_usd"])
    assert k["tokens"] == telas.tokens(sum(d["tokens"].values()))
    assert k["cache"] == telas.pct(d["cache_taxa"])
    assert k["chamadas"] == telas.inteiro(d["chamadas"]) == "382"
    assert k["tools"] == telas.inteiro(d["tools"]["chamadas"])
    assert k["ativo"] == telas.duracao(d["ativo_s"])
    assert len(re.findall(r'<a class="sessao-link"', corpo)) == len(d["sessoes"]) == 5
    assert corpo.count('class="selo-andamento"') == 1                    # só a sessão das 10:58
    assert "cache baixo · 3 pausas longas" in corpo
    for s in d["sessoes"]:
        assert telas.usd(s["custo_usd"]) in corpo


def _banco(tmp_path, projeto="proj", modelo="claude-sonnet-5-5", turno=1, categoria="teste_falhou") -> sqlite3.Connection:
    con = db.conectar(tmp_path / "t.db")
    ts = "2026-11-22T13:00:00.000Z"
    con.execute("""INSERT INTO chamadas (id, ferramenta, sessao_id, projeto, ts, modelo, input_tokens, output_tokens,
                   cache_read, custo_usd, origem_custo, arquivo, turno, seq)
                   VALUES ('c1', 'claude_code', 's1', ?, ?, ?, 10, 100, 1000, ?, ?, 'f', ?, 1)""",
                (projeto, ts, modelo, None if modelo.startswith("x") else 0.01,
                 "sem_preco" if modelo.startswith("x") else "calculado", turno))
    con.execute("""INSERT INTO tool_calls (id, ferramenta, sessao_id, projeto, ts, tool, erro, arquivo, erro_categoria, turno, seq)
                   VALUES ('t1', 'claude_code', 's1', ?, ?, '<b>Tool</b>', 1, 'f', ?, ?, 2)""", (projeto, ts, categoria, turno))
    con.execute("INSERT INTO eventos VALUES ('claude_code', 's1', ?, ?, 'atividade', 'f', NULL)", (projeto, ts))
    con.commit()
    return con


def _pagina(con, demo_=False) -> str:
    momento = datetime(2026, 11, 22, 14, 0, tzinfo=timezone.utc)
    d = consultas.dia(con, momento.astimezone().date(), momento, LIM)
    return telas.hoje(d, LIM, demo_, 5, "127.0.0.1:8765")


def test_texto_do_banco_e_escapado(tmp_path):
    con = _banco(tmp_path, projeto='<script>alert("x")</script>&"\'')
    corpo = _pagina(con)
    assert "<script" not in corpo.lower() and "<b>Tool</b>" not in corpo
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;&quot;&#x27;" in corpo
    assert "&lt;b&gt;Tool&lt;/b&gt;" in corpo


def test_modo_real_recarrega_a_cada_5_s(tmp_path):
    corpo = _pagina(_banco(tmp_path))
    assert '<meta http-equiv="refresh" content="5">' in corpo and "Ao vivo" in corpo


def test_dia_sem_sessoes(tmp_path):
    con = db.conectar(tmp_path / "vazio.db")
    corpo = _pagina(con)
    assert "Nenhuma sessão hoje" in corpo and "Sem custo hoje." in corpo and "Nenhuma tool hoje." in corpo
    assert _kpis(corpo)["custo"] == "US$ 0,00" and _kpis(corpo)["chamadas"] == "0"


def test_modelo_sem_preco_ganha_asterisco(tmp_path):
    corpo = _pagina(_banco(tmp_path, modelo="xmodelo-novo"))
    assert _kpis(corpo)["custo"].endswith("*")
    assert "sem preço" in corpo


def test_banco_sem_as_colunas_novas_avisa(tmp_path):
    corpo = _pagina(_banco(tmp_path, turno=None, categoria=None))
    assert "versão antiga do tc" in corpo and "tc ingest --tudo" in corpo
    assert "1 chamada(s) sem turno e sequência" in corpo and "1 erro(s) de tool sem categoria" in corpo


def test_recusa_nao_conta_como_erro_na_tela(tmp_path):
    corpo = _pagina(_banco(tmp_path, categoria="recusado_usuario"))
    assert "nenhuma com erro" in corpo and "1 recusada(s) por você" in corpo


# ---------- formatação ----------

@pytest.mark.parametrize("v, texto", [(4.87, "US$ 4,87"), (1234.5, "US$ 1.234,50"), (0.004, "< US$ 0,01"), (0, "US$ 0,00"),
                                      (None, "—")])
def test_usd(v, texto):
    assert telas.usd(v) == texto


@pytest.mark.parametrize("n, texto", [(3_204_000, "3,20 M"), (312_400, "312 k"), (950, "950"), (2_500_000_000, "2,50 B")])
def test_tokens(n, texto):
    assert telas.tokens(n) == texto


@pytest.mark.parametrize("s, texto", [(40, "40 s"), (58 * 60, "58 min"), (2 * 3600 + 11 * 60, "2h 11m")])
def test_duracao(s, texto):
    assert telas.duracao(s) == texto


@pytest.mark.parametrize("modelo, longo, curto", [("claude-opus-5-5", "Claude Opus 5.5", "Opus 5.5"),
                                                  ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "Haiku 4.5"),
                                                  ("claude-sonnet-4", "Claude Sonnet 4", "Sonnet 4"),
                                                  ("<synthetic>", "<synthetic>", "<synthetic>")])
def test_nome_do_modelo(modelo, longo, curto):
    assert telas.nome_do_modelo(modelo) == longo and telas.nome_do_modelo(modelo, curto=True) == curto


def test_preencher_escapa_tudo_que_nao_e_html():
    assert telas.preencher("chip", rotulo="<a>", valor=telas.Html("<b>ok</b>")) == \
        '<span class="chip">&lt;a&gt;: <strong><b>ok</b></strong></span>'


def test_custo_por_tipo_soma_o_custo_calculado(banco_demo):
    con = servidor.conectar_leitura(banco_demo)
    try:
        f = consultas.Filtro(desde="2026-11-22", ate="2026-11-22")
        cpt = consultas.custo_por_tipo(con, f)
        assert cpt["calculado_usd"] == pytest.approx(consultas.custo_total(con, f))
        assert cpt["registrado_usd"] == 0 and cpt["sem_cache_usd"] > cpt["calculado_usd"]
    finally:
        con.close()
