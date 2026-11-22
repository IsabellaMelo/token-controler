"""T22 e T23: colunas de sequência (turno, seq, chamada_id, subagente_id, servidor_mcp) e de erro (erro_categoria, exit_code).

O log é sintético e montado aqui. As mensagens de erro levam marcas (SEGREDO_*) para provar que o texto nunca chega ao banco
nem à saída dos comandos.
"""

import json
import sqlite3

import pytest

from token_controler import db
from token_controler.cli import main
from token_controler.fontes import claude_code

SESSAO = "sess-erro-0001"
MARCAS = ["SEGREDO_PROMPT_UM", "SEGREDO_PROMPT_DOIS", "SEGREDO_ERRO_TESTE", "SEGREDO_ERRO_MCP", "SEGREDO_ERRO_EDIT",
          "SEGREDO_ERRO_BLOQ", "SEGREDO_ERRO_RECUSA", "SEGREDO_ERRO_SUB", "SEGREDO_ERRO_OUTRO", "SEGREDO_AGENTE"]
USO = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
       "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}


def hora(s):
    return f"2026-11-22T10:{s // 60:02d}:{s % 60:02d}.000Z"


def prompt(seg, texto):
    return {"type": "user", "sessionId": SESSAO, "cwd": "C:\\x\\proj", "timestamp": hora(seg),
            "message": {"role": "user", "content": texto}}


def assistente(seg, msg, tools, sub=False, agente=None):
    linha = {"type": "assistant", "sessionId": SESSAO, "cwd": "C:\\x\\proj", "timestamp": hora(seg), "requestId": "req-" + msg,
             "isSidechain": sub,
             "message": {"id": msg, "model": "claude-opus-5-5", "usage": USO,
                         "content": [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in tools]}}
    if agente:
        linha["agentId"] = agente
    return linha


def resultado(seg, tid, texto="ok", erro=False, extra=None, sub=False):
    linha = {"type": "user", "sessionId": SESSAO, "cwd": "C:\\x\\proj", "timestamp": hora(seg), "isSidechain": sub,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": texto,
                                                      **({"is_error": True} if erro else {})}]}}
    linha.update(extra or {})
    return linha


def gravar(caminho, linhas):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text("\n".join(json.dumps(l) for l in linhas) + "\n", encoding="utf-8")


@pytest.fixture
def logs(tmp_path):
    """Duas rodadas: turno 1 com 5 falhas de tipos diferentes; turno 2 abre um subagente (agentId no resultado)
    e depois outro cujo vínculo só existe no .meta.json."""
    raiz = tmp_path / "logs" / "projects" / "proj"
    principal = [
        prompt(0, "SEGREDO_PROMPT_UM"),
        assistente(1, "m1", [("tB", "Bash", {"command": "pytest"}),
                             ("tM", "mcp__google-drive__search_files", {"query": "x"}),
                             ("tE", "Edit", {"file_path": "C:\\x\\a.py", "old_string": "a", "new_string": "b"})]),
        resultado(3, "tB", "Exit code 1\nFAILED tests/t.py - AssertionError SEGREDO_ERRO_TESTE", erro=True),
        resultado(4, "tM", "401 Unauthorized SEGREDO_ERRO_MCP", erro=True),
        resultado(5, "tE", "String to replace not found in file. SEGREDO_ERRO_EDIT", erro=True),
        assistente(6, "m2", [("tW", "WebSearch", {"query": "q"}), ("tP", "PowerShell", {"command": "ls"}),
                             ("tO", "Bash", {"command": "falha-estranha"}), ("tOK", "Bash", {"command": "echo ok"})]),
        resultado(7, "tW", "SEGREDO_ERRO_RECUSA", erro=True, extra={"toolDenialKind": "user-rejected"}),
        resultado(8, "tP", "SEGREDO_ERRO_BLOQ", erro=True, extra={"toolDenialKind": "permission-rule"}),
        resultado(9, "tO", "Exit code 7\nSEGREDO_ERRO_OUTRO nenhuma regra conhece", erro=True),
        resultado(10, "tOK", "tudo certo"),
        prompt(20, "SEGREDO_PROMPT_DOIS"),
        assistente(21, "m3", [("tA1", "Agent", {"prompt": "SEGREDO_AGENTE"})]),
        assistente(40, "m4", [("tA2", "Agent", {"prompt": "SEGREDO_AGENTE"})]),
        resultado(30, "tA1", "pronto", extra={"toolUseResult": {"agentId": "aaa111", "status": "completed"}}),
        resultado(50, "tA2", "pronto"),                                  # sem agentId no resultado: só o .meta.json liga
        assistente(60, "m5", [("tR", "Read", {"file_path": "C:\\x\\b.py"})]),
        resultado(61, "tR", "conteúdo"),
    ]
    gravar(raiz / f"{SESSAO}.jsonl", principal)
    sub1 = [
        assistente(22, "s1", [("tS1", "Read", {"file_path": "C:\\x\\c.py"})], sub=True, agente="aaa111"),
        resultado(23, "tS1", "SEGREDO_ERRO_SUB No such file or directory", erro=True, sub=True),
    ]
    sub2 = [assistente(41, "s2", [("tS2", "Bash", {"command": "ls"})], sub=True, agente="bbb222"),
            resultado(42, "tS2", "ok", sub=True)]
    pasta_sub = raiz / SESSAO / "subagents"
    gravar(pasta_sub / "agent-aaa111.jsonl", sub1)
    gravar(pasta_sub / "agent-bbb222.jsonl", sub2)
    (pasta_sub / "agent-aaa111.meta.json").write_text(json.dumps({"toolUseId": "tA1"}), encoding="utf-8")
    (pasta_sub / "agent-bbb222.meta.json").write_text(json.dumps({"toolUseId": "tA2"}), encoding="utf-8")
    return tmp_path / "logs"


@pytest.fixture
def con(tmp_path, logs):
    c = db.conectar(tmp_path / "e.db")
    claude_code.ingerir(c, pasta=logs / "projects")
    yield c
    c.close()


def tool(con, tid):
    return con.execute("SELECT * FROM tool_calls WHERE id = ?", (f"claude_code:{tid}",)).fetchone()


# ---------- T23: categoria e exit code ----------

@pytest.mark.parametrize("tid, categoria, saida", [
    ("tB", "teste_falhou", 1),
    ("tM", "mcp_auth", None),
    ("tE", "edicao_invalida", None),
    ("tW", "recusado_usuario", None),
    ("tP", "bloqueado_permissao", None),
    ("tO", "outro", 7),
    ("tS1", "arquivo_ausente", None),
    ("tOK", None, None),
    ("tR", None, None),
])
def test_categoria_e_exit_code_gravados(con, tid, categoria, saida):
    t = tool(con, tid)
    assert t["erro_categoria"] == categoria
    assert t["exit_code"] == saida


def test_nenhum_texto_de_erro_vai_para_o_banco(con, tmp_path):
    con.commit()
    conteudo = (tmp_path / "e.db").read_bytes()
    for marca in MARCAS:
        assert marca.encode() not in conteudo, marca
    # nem as frases das mensagens: só o rótulo e um inteiro foram gravados
    for frase in ("Unauthorized", "String to replace", "AssertionError", "No such file", "nenhuma regra conhece"):
        assert frase.encode() not in conteudo, frase
    # e nenhuma coluna de texto das tool_calls tem mais que rótulos/identificadores
    colunas = [r["name"] for r in con.execute("PRAGMA table_info(tool_calls)")]
    for coluna in ("erro_categoria", "exit_code", "servidor_mcp", "subagente_id", "chamada_id"):
        assert coluna in colunas
    valores = {r[0] for r in con.execute("SELECT DISTINCT erro_categoria FROM tool_calls WHERE erro_categoria IS NOT NULL")}
    from token_controler.regras_erros import CATEGORIAS
    assert valores <= set(CATEGORIAS)


def test_nenhum_texto_de_erro_na_saida_dos_comandos(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "saida.db")
    comandos = [["ingest"], ["ingest", "--tudo"], ["resumo"], ["resumo", "--por", "sessao"], ["sessoes"], ["tools"],
                ["comportamento"], ["diagnostico"], ["qualidade"], ["sessao", SESSAO]]
    for cmd in comandos + [c + ["--json"] for c in comandos[2:-1]]:
        main(["--db", banco, *cmd])
    capturado = capsys.readouterr()
    for marca in MARCAS + ["Unauthorized", "String to replace", "AssertionError", "nenhuma regra conhece"]:
        assert marca not in capturado.out and marca not in capturado.err, marca


def test_a_mensagem_nao_sobrevive_na_memoria_de_um_erro_de_ingestao(tmp_path, logs, monkeypatch):
    """Se a gravação falhar, a mensagem de erro não pode aparecer na exceção."""
    c = db.conectar(tmp_path / "x.db")
    c.execute("ALTER TABLE tool_calls RENAME COLUMN erro_categoria TO outra")
    with pytest.raises(sqlite3.Error) as e:
        claude_code.ingerir(c, pasta=logs / "projects")
    assert not any(m in str(e.value) for m in MARCAS)
    c.close()


def test_ingest_tudo_reclassifica(con, logs):
    con.execute("UPDATE tool_calls SET erro_categoria = NULL, exit_code = NULL")
    con.commit()
    claude_code.ingerir(con, pasta=logs / "projects", tudo=True)
    assert tool(con, "tB")["erro_categoria"] == "teste_falhou" and tool(con, "tB")["exit_code"] == 1
    assert tool(con, "tO")["exit_code"] == 7


def test_ingest_sem_tudo_nao_reclassifica_o_que_ja_foi_lido(con, logs):
    con.execute("UPDATE tool_calls SET erro_categoria = NULL")
    con.commit()
    claude_code.ingerir(con, pasta=logs / "projects")           # arquivos iguais: pulados
    assert tool(con, "tB")["erro_categoria"] is None


def test_erro_sem_o_campo_de_negacao_em_log_antigo_usa_o_texto(tmp_path):
    raiz = tmp_path / "l" / "projects" / "p"
    gravar(raiz / f"{SESSAO}.jsonl", [
        prompt(0, "oi"), assistente(1, "m1", [("t1", "Bash", {"command": "x"})]),
        resultado(2, "t1", "The user doesn't want to proceed with this tool use. The tool use was rejected.", erro=True)])
    c = db.conectar(tmp_path / "a.db")
    claude_code.ingerir(c, pasta=tmp_path / "l" / "projects")
    assert tool(c, "t1")["erro_categoria"] == "recusado_usuario"
    c.close()


# ---------- recusa fora da taxa de erro ----------

def test_tc_tools_nao_conta_a_recusa_como_erro(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "t.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "tools", "--json"])
    por = {t["tool"]: t for t in json.loads(capsys.readouterr().out)}
    assert por["WebSearch"]["chamadas"] == 1 and por["WebSearch"]["erros"] == 0       # só foi recusada
    assert por["WebSearch"]["recusadas"] == 1 and por["WebSearch"]["categorias"] == {}
    assert por["Bash"]["erros"] == 2 and por["Bash"]["categorias"] == {"teste_falhou": 1, "outro": 1}
    assert por["PowerShell"]["erros"] == 1 and por["PowerShell"]["categorias"] == {"bloqueado_permissao": 1}


def test_tc_tools_mostra_a_contagem_por_categoria(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "t.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "tools"])
    saida = capsys.readouterr().out
    assert "recusadas" in saida and "Erros por categoria" in saida
    for cat in ("teste_falhou", "mcp_auth", "edicao_invalida", "bloqueado_permissao", "arquivo_ausente", "outro"):
        assert cat in saida
    assert "recusado_usuario" not in saida.split("Erros por categoria")[1]              # as recusas ficam fora da lista


def test_tc_resumo_e_sessoes_nao_contam_a_recusa(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "t.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "sessoes", "--json"])
    s = json.loads(capsys.readouterr().out)[0]
    assert s["erros_tool"] == 6          # 7 com is_error, menos a recusa do WebSearch
    main(["--db", banco, "resumo", "--por", "projeto", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["erros_tool"] == 6


def test_a_recusa_nao_entra_na_corrida_de_falhas(con):
    from token_controler import diagnostico
    corrida = diagnostico.corridas_de_falhas(
        [{"tool": "Bash", "erro": 1, "erro_categoria": c} for c in ("recusado_usuario", None, "outro")], {"loop_min": 3})
    assert corrida == []
    assert len(diagnostico.corridas_de_falhas(
        [{"tool": "Bash", "erro": 1, "erro_categoria": c} for c in ("teste_falhou", None, "outro")], {"loop_min": 3})) == 1


# ---------- T22: sequência ----------

def test_turnos(con):
    assert [tool(con, t)["turno"] for t in ("tB", "tM", "tE", "tW", "tOK")] == [1, 1, 1, 1, 1]
    assert [tool(con, t)["turno"] for t in ("tA1", "tA2", "tR")] == [2, 2, 2]
    assert {r[0] for r in con.execute("SELECT turno FROM chamadas WHERE subagente = 0 AND id LIKE '%:m1:%' OR id LIKE '%:m2:%'")} == {1}
    assert con.execute("SELECT turno FROM chamadas WHERE id LIKE '%:m5:%'").fetchone()[0] == 2


def test_chamada_id_liga_a_tool_a_chamada_que_a_pediu(con):
    for tid, msg in (("tB", "m1"), ("tM", "m1"), ("tE", "m1"), ("tW", "m2"), ("tOK", "m2"), ("tR", "m5"), ("tS1", "s1")):
        cid = tool(con, tid)["chamada_id"]
        assert cid == f"claude_code:{msg}:req-{msg}"
        assert con.execute("SELECT COUNT(*) FROM chamadas WHERE id = ?", (cid,)).fetchone()[0] == 1


def test_servidor_mcp(con):
    assert tool(con, "tM")["servidor_mcp"] == "google-drive"
    assert tool(con, "tB")["servidor_mcp"] is None
    assert tool(con, "tM")["tool"] == "mcp__google-drive__search_files"        # o nome completo continua em `tool`


def test_subagente_ligado_pelo_resultado_e_pelo_meta(con):
    assert tool(con, "tA1")["subagente_id"] == "aaa111"                          # pelo toolUseResult.agentId
    assert tool(con, "tA2")["subagente_id"] == "bbb222"                          # só pelo .meta.json
    assert tool(con, "tS1")["subagente_id"] == "aaa111" and tool(con, "tS2")["subagente_id"] == "bbb222"
    assert tool(con, "tB")["subagente_id"] is None
    for cid, ag in (("s1", "aaa111"), ("s2", "bbb222")):
        assert con.execute("SELECT subagente_id FROM chamadas WHERE id LIKE ?", (f"%:{cid}:%",)).fetchone()[0] == ag
    assert con.execute("SELECT subagente_id FROM chamadas WHERE id LIKE '%:m1:%'").fetchone()[0] is None


def test_o_subagente_herda_o_turno_da_chamada_agent(con):
    assert tool(con, "tS1")["turno"] == 2 and tool(con, "tS2")["turno"] == 2
    assert con.execute("SELECT turno FROM chamadas WHERE id LIKE '%:s1:%'").fetchone()[0] == 2
    # subagente cuja ligação não existe fica com NULL, não com um turno inventado


def test_seq_e_unica_e_segue_o_horario_da_sessao_inteira(con):
    passos = [(r["ts"], r["seq"], r["id"]) for r in con.execute(
        "SELECT ts, seq, id FROM chamadas UNION ALL SELECT ts, seq, id FROM tool_calls")]
    seqs = sorted(p[1] for p in passos)
    assert seqs == list(range(1, len(passos) + 1))                                # 1..n, sem repetir nem pular
    por_ts = [p[1] for p in sorted(passos, key=lambda p: p[0])]
    assert por_ts == sorted(por_ts)                                               # a ordem por horário é a ordem por seq
    # o subagente aparece no meio da thread principal, não no fim
    assert tool(con, "tS1")["seq"] < tool(con, "tA2")["seq"] and tool(con, "tS1")["seq"] > tool(con, "tA1")["seq"]


def test_a_chamada_vem_antes_das_tools_que_ela_pediu(con):
    for tid, msg in (("tB", "m1"), ("tW", "m2"), ("tR", "m5")):
        c = con.execute("SELECT seq FROM chamadas WHERE id LIKE ?", (f"%:{msg}:%",)).fetchone()[0]
        assert c < tool(con, tid)["seq"]


def test_tools_pedidas_juntas_mantem_a_ordem_do_log(con):
    assert [tool(con, t)["seq"] for t in ("tB", "tM", "tE")] == sorted(tool(con, t)["seq"] for t in ("tB", "tM", "tE"))


def test_reingerir_so_o_subagente_mantem_as_ligacoes(con, logs):
    arq = logs / "projects" / "proj" / SESSAO / "subagents" / "agent-bbb222.jsonl"
    arq.write_text(arq.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    import os
    os.utime(arq, (arq.stat().st_atime + 5, arq.stat().st_mtime + 5))
    r = claude_code.ingerir(con, pasta=logs / "projects")
    assert r["arquivos_lidos"] == 1
    assert tool(con, "tA2")["subagente_id"] == "bbb222" and tool(con, "tS2")["turno"] == 2
    n = con.execute("SELECT COUNT(*) FROM chamadas").fetchone()[0] + con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    assert sorted(r[0] for r in con.execute("SELECT seq FROM chamadas UNION ALL SELECT seq FROM tool_calls")) == list(range(1, n + 1))


# ---------- migração ----------

def test_banco_antigo_ganha_as_colunas_novas_com_null(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE tool_calls (id TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL, projeto TEXT,
            ts TEXT NOT NULL, tool TEXT NOT NULL, erro INTEGER, subagente INTEGER NOT NULL DEFAULT 0, arquivo TEXT NOT NULL);
        INSERT INTO tool_calls VALUES ('claude_code:x', 'claude_code', 's', 'p', '2026-11-01T00:00:00Z', 'Bash', 1, 0, 'f');
        CREATE TABLE chamadas (id TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL, projeto TEXT,
            ts TEXT NOT NULL, modelo TEXT, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_5m INTEGER NOT NULL DEFAULT 0, cache_write_1h INTEGER NOT NULL DEFAULT 0,
            cache_read INTEGER NOT NULL DEFAULT 0, custo_usd REAL, subagente INTEGER NOT NULL DEFAULT 0,
            erro_api INTEGER NOT NULL DEFAULT 0, arquivo TEXT NOT NULL);
        INSERT INTO chamadas (id, ferramenta, sessao_id, ts, arquivo) VALUES ('c1', 'claude_code', 's', '2026-11-01T00:00:00Z', 'f');
    """)
    antigo.commit()
    antigo.close()
    c = db.conectar(caminho)
    cols_t = {r["name"] for r in c.execute("PRAGMA table_info(tool_calls)")}
    cols_c = {r["name"] for r in c.execute("PRAGMA table_info(chamadas)")}
    assert {"turno", "seq", "chamada_id", "subagente_id", "servidor_mcp", "exit_code", "erro_categoria"} <= cols_t
    assert {"turno", "seq", "subagente_id"} <= cols_c
    assert "alvo_ext" not in cols_t                                               # PENDENTE de decisão: não existe
    antigo_t = c.execute("SELECT * FROM tool_calls").fetchone()
    assert antigo_t["erro"] == 1 and antigo_t["erro_categoria"] is None and antigo_t["turno"] is None
    # erro antigo, ainda sem categoria, continua contando como erro
    assert c.execute(f"SELECT SUM({db.ERRO_REAL}) FROM tool_calls").fetchone()[0] == 1
    db.conectar(caminho).close()                                                  # abrir de novo não repete a migração
    c.close()


def test_alvo_ext_nao_existe_no_schema_novo(con):
    assert "alvo_ext" not in {r["name"] for r in con.execute("PRAGMA table_info(tool_calls)")}


# ---------- tc qualidade ----------

def test_qualidade_conta_os_erros_em_outro(con, tmp_path, logs, monkeypatch, capsys):
    from token_controler import qualidade
    q = qualidade.coletar(con)[0]
    assert q["erros_tool"] == 6 and q["erros_tool_outro"] == 1 and q["erros_tool_sem_categoria"] == 0   # a recusa fica fora
    con.execute("UPDATE tool_calls SET erro_categoria = NULL")
    q = qualidade.coletar(con)[0]
    assert q["erros_tool_sem_categoria"] == 7 and q["erros_tool_outro"] == 0                            # sem categoria: a recusa conta
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "q.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "qualidade"])
    saida = capsys.readouterr().out
    assert "sem categoria definida (outro): 1 de 6" in saida


def test_tc_sessao_marca_a_recusa_e_nao_como_falha(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "s.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "sessao", SESSAO])
    saida = capsys.readouterr().out
    linha_web = next(l for l in saida.splitlines() if "WebSearch" in l)
    assert "recusado" in linha_web and "✗" not in linha_web
    assert "✗" in next(l for l in saida.splitlines() if "PowerShell" in l)          # o bloqueio por regra é uma falha
