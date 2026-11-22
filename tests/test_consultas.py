"""consultas.py: filtro comum, turnos e sequência de um turno (banco sintético, montado à mão)."""

from __future__ import annotations

from datetime import date

import pytest

from token_controler import consultas, db

S = "sess-seq"
ARQ = "sintetico.jsonl"


def _chamada(con, id_, ts, turno, seq, custo=0.01, modelo="claude-sonnet-5-5", subagente=0, subagente_id=None,
             erro_api=0):
    con.execute("""INSERT INTO chamadas (id, ferramenta, sessao_id, projeto, ts, modelo, input_tokens, output_tokens,
                   cache_write_5m, cache_write_1h, cache_read, custo_usd, subagente, erro_api, arquivo, origem_custo,
                   turno, seq, subagente_id)
                   VALUES (?, 'claude_code', ?, 'proj', ?, ?, 10, 20, 0, 30, 40, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (id_, S, ts, modelo, custo, subagente, erro_api, ARQ,
                 None if erro_api else ("sem_preco" if custo is None else "calculado"), turno, seq, subagente_id))


def _tool(con, id_, ts, turno, seq, tool="Read", erro=0, categoria=None, duracao_ms=100, chamada_id=None, **extra):
    colunas = {"alvo_hash": None, "subtipo": None, "servidor_mcp": None, "exit_code": None, "subagente": 0,
               "subagente_id": None, "alvo_nome": None, **extra}
    con.execute(f"""INSERT INTO tool_calls (id, ferramenta, sessao_id, projeto, ts, tool, erro, arquivo, duracao_ms,
                    turno, seq, chamada_id, erro_categoria, {", ".join(colunas)})
                    VALUES (?, 'claude_code', ?, 'proj', ?, ?, ?, ?, ?, ?, ?, ?, ?, {", ".join("?" * len(colunas))})""",
                (id_, S, ts, tool, erro, ARQ, duracao_ms, turno, seq, chamada_id, categoria, *colunas.values()))


def _prompt(con, ts, rotulo=None):
    con.execute("INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo, rotulo) "
                "VALUES ('claude_code', ?, 'proj', ?, 'prompt', ?, ?)", (S, ts, ARQ, rotulo))


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def sessao(con):
    """Turno 0 (antes do 1º prompt), turno 1 com subagente, MCP, uma recusa e uma falha, e turno 2 sem chamada."""
    _chamada(con, "c0", "2026-11-22T09:59:00.000Z", 0, 1)
    _prompt(con, "2026-11-22T10:00:00.000Z", rotulo="debug")
    _chamada(con, "c1", "2026-11-22T10:00:02.000Z", 1, 2, custo=0.10)
    _tool(con, "t1", "2026-11-22T10:00:03.000Z", 1, 3, tool="Read", chamada_id="c1", alvo_hash="abc123",
          alvo_nome="segredo.py", duracao_ms=500)
    _tool(con, "t2", "2026-11-22T10:00:03.000Z", 1, 4, tool="Bash", erro=1, categoria="teste_falhou", chamada_id="c1",
          subtipo="teste", exit_code=1, duracao_ms=9000)       # termina às 10:00:12, depois da última linha do turno
    _tool(con, "t3", "2026-11-22T10:00:04.000Z", 1, 5, tool="Bash", erro=1, categoria="recusado_usuario",
          chamada_id="c1")
    _tool(con, "t4", "2026-11-22T10:00:05.000Z", 1, 6, tool="mcp__drive__buscar", servidor_mcp="drive", chamada_id="c1")
    _tool(con, "t5", "2026-11-22T10:00:06.000Z", 1, 7, tool="Agent", chamada_id="c1", subagente_id="ag1")
    _chamada(con, "c2", "2026-11-22T10:00:07.000Z", 1, 8, custo=0.05, subagente=1, subagente_id="ag1")
    _tool(con, "t6", "2026-11-22T10:00:08.000Z", 1, 9, tool="Grep", chamada_id="c2", subagente=1, subagente_id="ag1")
    _chamada(con, "c3", "2026-11-22T10:00:10.000Z", 1, 10, custo=None, modelo="modelo-sem-preco")
    _prompt(con, "2026-11-22T10:05:00.000Z")                  # interrompido: nenhuma chamada no turno 2
    con.commit()
    return con


# ---------- filtro ----------

def test_filtro_sem_nada_e_tudo():
    assert consultas.Filtro().sql() == ("1=1", [])


def test_filtro_combina_na_ordem_de_sempre():
    cond, p = consultas.Filtro(desde="2026-11-01", ate="2026-11-22", projeto="x", ferramenta="claude_code").sql()
    assert cond == ("date(ts, 'localtime') >= ? AND date(ts, 'localtime') <= ? AND projeto = ? AND ferramenta = ?")
    assert p == ["2026-11-01", "2026-11-22", "x", "claude_code"]


@pytest.mark.parametrize("periodo, desde", [("hoje", "2026-11-22"), ("7d", "2026-11-16"), ("30d", "2026-10-24"),
                                            ("mes", "2026-11-01"), ("tudo", None)])
def test_filtro_do_periodo(periodo, desde):
    f = consultas.filtro_do_periodo(periodo, date(2026, 11, 22), projeto="p")
    assert (f.desde, f.ate, f.projeto) == (desde, None, "p")


def test_painel_e_live_usam_as_mesmas_consultas():
    from token_controler import agora
    assert agora.custo_de_hoje is consultas.por_hora
    pytest.importorskip("rich")
    from token_controler import painel
    assert painel.coletar is consultas.periodo and painel.inicio_do_periodo is consultas.inicio_do_periodo


# ---------- turnos ----------

def test_turnos_da_sessao(sessao):
    info = consultas.turnos(sessao, S)
    assert info["disponivel"] is True
    t0, t1, t2 = info["turnos"]
    assert (t0["turno"], t0["prompt_ts"], t0["chamadas"], t0["tools"]) == (0, None, 1, 0)
    assert t1["prompt_ts"] == "2026-11-22T10:00:00.000Z" and t1["inicio"] == t1["prompt_ts"]
    assert (t1["chamadas"], t1["tools"]) == (3, 6)
    assert (t1["erros"], t1["recusadas"]) == (1, 1)              # a recusa não conta como erro (T23)
    assert t1["custo_usd"] == pytest.approx(0.15) and t1["sem_preco"] == 1
    assert (t2["turno"], t2["chamadas"], t2["tools"], t2["custo_usd"]) == (2, 0, 0, None)
    assert t2["inicio"] == t2["fim"] == "2026-11-22T10:05:00.000Z"


def test_turnos_sem_as_colunas_da_t22(con):
    con.execute("""INSERT INTO chamadas (id, ferramenta, sessao_id, ts, arquivo) VALUES ('x', 'claude_code', ?, ?, ?)""",
                (S, "2026-11-22T10:00:00.000Z", ARQ))
    assert consultas.turnos(con, S) == {"sessao": S, "disponivel": False, "turnos": []}
    assert consultas.sequencia(con, S, 1)["disponivel"] is False


# ---------- sequência ----------

def test_sequencia_em_ordem_com_inicio_e_fim_no_turno(sessao):
    seq = consultas.sequencia(sessao, S, 1)
    assert seq["existe"] and seq["disponivel"]
    tipos = [(p["tipo"], p.get("tool") or p.get("modelo") or p.get("rotulo")) for p in seq["passos"]]
    assert tipos == [("prompt", "debug"), ("modelo", "claude-sonnet-5-5"), ("tool", "Read"), ("tool", "Bash"),
                     ("tool", "Bash"), ("tool", "mcp__drive__buscar"), ("tool", "Agent"), ("modelo", "claude-sonnet-5-5"),
                     ("tool", "Grep"), ("modelo", "modelo-sem-preco")]
    assert [p["inicio_ms"] for p in seq["passos"]][:3] == [0, 2000, 3000]
    # o fim do turno vai até o fim da tool mais longa (Bash de 9 s começando às 10:00:03)
    assert seq["fim"] == "2026-11-22T10:00:12Z" and seq["duracao_ms"] == 12000


def test_sequencia_marca_erro_recusa_mcp_e_subagente(sessao):
    por_id = {p.get("id"): p for p in consultas.sequencia(sessao, S, 1)["passos"]}
    assert por_id["t2"]["erro_real"] and por_id["t2"]["erro_categoria"] == "teste_falhou" and por_id["t2"]["exit_code"] == 1
    assert por_id["t2"]["subtipo"] == "teste"
    assert por_id["t3"]["erro"] == 1 and not por_id["t3"]["erro_real"]          # recusa: passo neutro
    assert por_id["t4"]["servidor_mcp"] == "drive"
    assert por_id["t5"]["subagente_id"] == "ag1" and not por_id["t5"]["no_subagente"]
    assert por_id["c2"]["no_subagente"] and por_id["t6"]["no_subagente"]
    assert por_id["t1"]["chamada_id"] == "c1" and por_id["c1"]["custo_usd"] == pytest.approx(0.10)
    assert por_id["c3"]["custo_usd"] is None and por_id["c3"]["origem_custo"] == "sem_preco"


def test_sequencia_so_identifica_arquivo_pelo_hash(sessao):
    """O nome do arquivo (alvo_nome) existe no banco, mas a sequência não o devolve: só o hash (decisão da Etapa 4)."""
    passos = consultas.sequencia(sessao, S, 1)["passos"]
    leitura = next(p for p in passos if p.get("id") == "t1")
    assert leitura["alvo_hash"] == "abc123"
    assert "alvo_nome" not in leitura
    assert "segredo.py" not in repr(passos)


def test_sequencia_de_turno_inexistente(sessao):
    seq = consultas.sequencia(sessao, S, 99)
    assert seq["existe"] is False and seq["passos"] == []


def test_sequencia_de_turno_sem_chamada_mostra_so_o_prompt(sessao):
    seq = consultas.sequencia(sessao, S, 2)
    assert [p["tipo"] for p in seq["passos"]] == ["prompt"] and seq["duracao_ms"] == 0
