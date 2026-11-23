"""Rótulo `debug` derivado do prompt: só o rótulo é gravado; o texto não vai para o banco nem para a saída."""

import re
import json
import sqlite3

import pytest

from token_controler import comportamento as cp
from token_controler import db
from token_controler.cli import main
from token_controler.fontes import claude_code
from token_controler.rotulos import rotular_prompt

MARCA = "XYZSEGREDO"  # aparece em todo prompt de teste: não pode surgir em lugar nenhum fora do log


# ---------- o rotulador ----------

@pytest.mark.parametrize("texto", [
    "corrija o erro no relatorio", "Corrige isso, por favor", "deu erro 500 na API", "ERROS no login",
    "o servidor travou", "não funciona o botão", "Não está funcionando", "parou de funcionar ontem",
    "tem um bug aqui", "exceção no parser", "isso quebrou", "a build falhou", "qual é o problema?",
    "resultado inesperado", "fix the failing test", "this is broken", "it crashes on start", "not working",
    "doesn't work", "stack trace abaixo", "Traceback (most recent call last)", "vamos depurar", "debugging session",
    "unexpected output", "that's wrong",
])
def test_rotula_debug(texto):
    assert rotular_prompt(texto) == "debug"


@pytest.mark.parametrize("texto", [
    "adicione um botao novo", "explique como funciona o cache", "crie um relatorio mensal", "refatore esta funcao",
    "add a new feature", "show me the list", "fixture do pytest", "prefixo do arquivo", "terrores noturnos",
    "o erróneo", "", "   ", None, 123, ["corrija o erro"], {"text": "erro"},
])
def test_nao_rotula(texto):
    assert rotular_prompt(texto) is None


def test_ignora_acento_e_caixa():
    assert rotular_prompt("CORRIJA") == rotular_prompt("corríja") == rotular_prompt("Exceção") == "debug"


# ---------- do log ao banco ----------

def _linha(tipo, ts, conteudo, **extra):
    return {"type": tipo, "sessionId": "sess-ffff-6666", "cwd": "C:\\Users\\usuario\\proj\\rot", "timestamp": ts,
            "message": {"role": "user", "content": conteudo}, **extra}


def _resposta(n, ts, tools):
    blocos = [{"type": "tool_use", "id": f"t{n}-{i}", "name": nome, "input": entrada}
              for i, (nome, entrada) in enumerate(tools)]
    return {"type": "assistant", "sessionId": "sess-ffff-6666", "cwd": "C:\\Users\\usuario\\proj\\rot", "timestamp": ts,
            "requestId": f"r{n}", "message": {"id": f"m{n}", "model": "claude-opus-5-5",
                                              "usage": {"input_tokens": 100, "output_tokens": 10}, "content": blocos}}


EDIT = ("Edit", {"file_path": "C:\\x\\a.py"})
PYTEST = ("Bash", {"command": "pytest -q"})
LER = ("Read", {"file_path": "C:\\x\\a.py"})
AGENTE = ("Agent", {"prompt": "x"})

TURNOS = [
    (f"{MARCA} corrija o erro", [EDIT]),                                             # 0 debug
    (f"{MARCA} adicione um botao novo", [EDIT]),                                     # 1 codificação
    (f"{MARCA} deu erro no teste", [PYTEST]),                                        # 2 testes: não é refinado
    (f"{MARCA} por que falhou?", [LER]),                                             # 3 exploração -> debug
    (f"{MARCA} esse erro e estranho", []),                                           # 4 sem tools: conversa
    (f"{MARCA} fix the bug", [AGENTE]),                                              # 5 delegação: não é refinado
    ([{"type": "text", "text": f"{MARCA} not working"}, {"type": "image", "source": {}}], [EDIT]),  # 6 debug
]


def montar(pasta):
    linhas, n = [], 0
    for i, (prompt, tools) in enumerate(TURNOS):
        minuto = i * 10
        linhas.append(_linha("user", f"2026-11-21T10:{minuto:02d}:00Z", prompt))
        if not tools:
            n += 1
            linhas.append(_resposta(n, f"2026-11-21T10:{minuto:02d}:05Z", []))
        for nome, entrada in tools:
            n += 1
            linhas.append(_resposta(n, f"2026-11-21T10:{minuto:02d}:0{min(n % 5 + 1, 9)}Z", [(nome, entrada)]))
            linhas.append(_linha("user", f"2026-11-21T10:{minuto:02d}:09Z",
                                 [{"type": "tool_result", "tool_use_id": f"t{n}-0", "content": f"{MARCA} saida"}]))
    pasta.mkdir(parents=True)
    (pasta / "s.jsonl").write_text("\n".join(json.dumps(l) for l in linhas) + "\n", encoding="utf-8")


@pytest.fixture
def logs(tmp_path):
    montar(tmp_path / "logs" / "projects" / "proj")
    entrada = (tmp_path / "logs" / "projects" / "proj" / "s.jsonl").read_text(encoding="utf-8")
    assert MARCA in entrada and "corrija o erro" in entrada   # o texto está no log: os testes de vazamento não são vazios
    return tmp_path / "logs"


@pytest.fixture
def con(tmp_path, logs):
    c = db.conectar(tmp_path / "r.db")
    claude_code.ingerir(c, pasta=logs / "projects")
    yield c
    c.close()


def test_so_prompts_digitados_recebem_rotulo(con):
    rotulos = [r[0] for r in con.execute("SELECT rotulo FROM eventos WHERE tipo = 'prompt' ORDER BY ts")]
    assert rotulos == ["debug", None, "debug", "debug", "debug", "debug", "debug"]
    assert con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'atividade' AND rotulo IS NOT NULL").fetchone()[0] == 0


def test_debug_refina_so_codificacao_e_exploracao(con):
    turnos = cp.montar_turnos(con, ["sess-ffff-6666"])
    assert [t.categoria for t in turnos] == [
        "debug", "codificacao", "testes", "debug", "conversa", "delegacao", "debug"]
    assert [t.rotulo for t in turnos] == ["debug", None, "debug", "debug", "debug", "debug", "debug"]


def test_sessao_e_one_shot_com_debug(con):
    r = cp.por_sessao(con, ["sess-ffff-6666"])["sess-ffff-6666"]
    assert r["categoria"] == "debug"                  # 3 turnos de debug contra 1 de cada outra categoria
    assert r["turnos_edicao"] == 3                    # turnos de debug com edição entram no one-shot
    por = {d["categoria"]: d for d in cp.por_categoria(cp.montar_turnos(con, ["sess-ffff-6666"]))}
    assert por["debug"]["turnos"] == 3 and por["debug"]["turnos_edicao"] == 2


def test_sem_rotulo_no_banco_antigo_nada_vira_debug(con):
    con.execute("UPDATE eventos SET rotulo = NULL")   # como num banco anterior a esta versão
    turnos = cp.montar_turnos(con, ["sess-ffff-6666"])
    assert [t.categoria for t in turnos] == [
        "codificacao", "codificacao", "testes", "exploracao", "conversa", "delegacao", "codificacao"]


def test_o_texto_do_prompt_nao_vai_para_o_banco(con, tmp_path):
    con.commit()
    conteudo = (tmp_path / "r.db").read_bytes()
    for texto in (MARCA, "corrija", "botao", "estranho", "falhou", "not working", "fix the bug", "saida"):
        # palavra inteira: o rótulo de categoria de erro `teste_falhou` (T23) contém "falhou" e é legítimo
        assert not re.search(rb"(?<![A-Za-z_])" + re.escape(texto.encode()) + rb"(?![A-Za-z_])", conteudo), texto


def test_o_texto_nao_aparece_na_saida_de_nenhum_comando(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "saida.db")
    comandos = [["ingest"], ["ingest", "--tudo"], ["resumo"], ["resumo", "--por", "sessao"], ["sessoes"], ["tools"],
                ["comportamento"], ["diagnostico"], ["qualidade"]]
    for cmd in comandos + [c + ["--json"] for c in comandos[2:]]:
        main(["--db", banco, *cmd])
    capturado = capsys.readouterr()
    for texto in (MARCA, "corrija", "botao", "estranho", "falhou", "not working", "fix the bug"):
        assert texto not in capturado.out and texto not in capturado.err, texto


def test_cli_mostra_a_categoria_debug(tmp_path, logs, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(logs))
    banco = str(tmp_path / "cli.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()
    main(["--db", banco, "comportamento", "--json"])
    assert "debug" in {d["categoria"] for d in json.loads(capsys.readouterr().out)}
    main(["--db", banco, "sessoes"])
    assert "Debug" in capsys.readouterr().out


def test_banco_antigo_ganha_a_coluna_rotulo_com_null(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE eventos (ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL, projeto TEXT, ts TEXT NOT NULL,
                              tipo TEXT NOT NULL, arquivo TEXT NOT NULL);
        INSERT INTO eventos VALUES ('claude_code', 's', 'p', '2026-11-01T00:00:00Z', 'prompt', 'f');
    """)
    antigo.commit()
    antigo.close()
    c = db.conectar(caminho)
    assert c.execute("SELECT rotulo FROM eventos").fetchone()[0] is None
    c.close()


def test_conteudo_de_prompt_estranho_nao_derruba_a_leitura(tmp_path):
    pasta = tmp_path / "logs" / "projects" / "proj"
    pasta.mkdir(parents=True)
    estranhos = [_linha("user", "2026-11-21T10:00:00Z", c) for c in
                 (None, 42, {"a": 1}, [None, 3, {"type": "text"}, {"type": "text", "text": 5}], [])]
    (pasta / "s.jsonl").write_text("\n".join(json.dumps(l) for l in estranhos) + "\n", encoding="utf-8")
    con = db.conectar(tmp_path / "e.db")
    claude_code.ingerir(con, pasta=tmp_path / "logs" / "projects")
    assert con.execute("SELECT COUNT(*) FROM eventos WHERE rotulo IS NOT NULL").fetchone()[0] == 0
