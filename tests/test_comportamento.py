"""T14 (categoria) e T15 (one-shot): regras puras e um log sintético montado no teste."""

import json
from pathlib import Path

import pytest

from token_controler import comportamento as cp
from token_controler import db
from token_controler.cli import main
from token_controler.comportamento import categoria_da_sessao, categoria_do_turno, contar_retries
from token_controler.fontes import claude_code

CUSTO = (1000 * 4 + 1000 * 20) / 1e6  # opus 5.5: cada linha de resposta custa US$ 0,024


def c(tool, subtipo=None, alvo=None):
    return {"tool": tool, "subtipo": subtipo, "alvo": alvo, "erro": None}


# ---------- regras puras ----------

@pytest.mark.parametrize("chamadas, esperada", [
    ([], "conversa"),
    ([c("Edit")], "codificacao"),
    ([c("Write"), c("Bash", "teste")], "codificacao"),             # com edição, o teste não muda a categoria
    ([c("Read"), c("Grep")], "exploracao"),
    ([c("WebSearch")], "exploracao"),
    ([c("mcp__alguma__ferramenta")], "exploracao"),
    ([c("Bash", "teste")], "testes"),
    ([c("Bash", "git"), c("Read")], "git"),                         # shell sem edição vence a leitura
    ([c("PowerShell", "build")], "build"),
    ([c("Bash", "leitura")], "exploracao"),
    ([c("Bash", "leitura"), c("Bash", "leitura")], "exploracao"),
    ([c("Bash", "outro")], "geral"),
    ([c("Bash", "outro"), c("Read")], "exploracao"),
    ([c("Agent"), c("Edit")], "delegacao"),                         # delegação vem antes da edição
    ([c("Task")], "delegacao"),
    ([c("EnterPlanMode"), c("Agent")], "planejamento"),
    ([c("TodoWrite")], "planejamento"),
    ([c("TodoWrite"), c("Edit")], "codificacao"),
    ([c("Skill")], "geral"),
    ([c("AskUserQuestion")], "geral"),
])
def test_categoria_do_turno(chamadas, esperada):
    assert categoria_do_turno(chamadas) == esperada


@pytest.mark.parametrize("chamadas, retries", [
    ([c("Edit", alvo="a"), c("Bash", "teste"), c("Edit", alvo="a")], 1),
    ([c("Edit", alvo="a"), c("Bash", "outro"), c("Edit", alvo="a")], 1),
    ([c("Edit", alvo="a"), c("Edit", alvo="a")], 0),                                  # sem shell no meio
    ([c("Edit", alvo="a"), c("Bash", "leitura"), c("Edit", alvo="a")], 0),            # shell de leitura não verifica
    ([c("Edit", alvo="a"), c("Bash", "teste"), c("Edit", alvo="b")], 0),              # outro arquivo
    ([c("Edit", alvo="a"), c("Bash", None), c("Edit", alvo="a")], 1),                 # sem classe: conservador
    ([c("Bash", "teste"), c("Edit", alvo="a"), c("Edit", alvo="a")], 0),              # shell antes das edições
    ([c("Edit", alvo="a"), c("Edit", alvo="a"), c("Bash", "teste")], 0),              # shell depois
    ([c("Edit", alvo="a"), c("Bash", "teste"), c("Edit", alvo="a"), c("Bash", "teste"), c("Edit", alvo="a")], 2),
    ([c("Edit", alvo=None), c("Bash", "teste"), c("Edit", alvo=None)], 1),            # sem alvo: tratado como o mesmo
    ([c("Write", alvo="a"), c("PowerShell", "outro"), c("Write", alvo="a")], 1),
    ([c("Read", alvo="a"), c("Bash", "teste"), c("Read", alvo="a")], 0),              # leitura não é edição
])
def test_contar_retries(chamadas, retries):
    assert contar_retries(chamadas) == retries


def turno(categoria, n_chamadas=0):
    t = cp.Turno("s", None, 0)
    t.categoria, t.chamadas = categoria, [c("Read")] * n_chamadas
    return t


def test_categoria_da_sessao_usa_a_mais_frequente_e_desempata_por_chamadas_e_ordem():
    assert categoria_da_sessao([turno("testes", 1), turno("git", 5)]) == "git"          # empate: mais tool calls
    assert categoria_da_sessao([turno("git", 2), turno("testes", 2)]) == "testes"       # empate total: ordem fixa
    assert categoria_da_sessao([turno("testes", 1), turno("testes", 1), turno("git", 9)]) == "testes"


def test_conversa_so_e_a_categoria_da_sessao_sem_nenhuma_tool():
    # turnos de bate-papo não disputam com os que usaram tools, mesmo sendo a maioria
    assert categoria_da_sessao([turno("conversa"), turno("conversa"), turno("codificacao", 1)]) == "codificacao"
    assert categoria_da_sessao([turno("conversa"), turno("conversa")]) == "conversa"
    assert categoria_da_sessao([]) == "conversa"


# ---------- log sintético montado no teste ----------

def montar_log(pasta: Path, turnos, antes=()):
    """Escreve um .jsonl. Cada turno = (lista de tools,) após um prompt; tool = (nome, input, subagente?)."""
    relogio = [0]
    linhas = []

    def ts():
        relogio[0] += 7
        s = relogio[0]
        return f"2026-11-21T{10 + s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}Z"

    base = {"sessionId": "sess-eeee-5555", "cwd": "C:\\Users\\usuario\\proj\\comp"}

    def assistente(tools):
        n = len(linhas)
        blocos = [{"type": "tool_use", "id": f"t{n}-{i}", "name": nome, "input": entrada}
                  for i, (nome, entrada, _) in enumerate(tools)]
        lado = any(sub for *_, sub in tools)
        linhas.append(json.dumps({**base, "type": "assistant", "timestamp": ts(), "requestId": f"r{n}",
                                  "isSidechain": lado, "message": {"id": f"m{n}", "model": "claude-opus-5-5",
                                  "usage": {"input_tokens": 1000, "output_tokens": 1000}, "content": blocos}}))

    for tools in antes:                     # atividade antes do primeiro prompt
        for t in tools:
            assistente([t])
    for tools in turnos:
        linhas.append(json.dumps({**base, "type": "user", "timestamp": ts(),
                                  "message": {"role": "user", "content": "SEGREDO pedido do usuario"}}))
        if not tools:
            assistente([])                  # resposta só com texto
        for t in tools:
            assistente([t])
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "s.jsonl").write_text("\n".join(linhas) + "\n", encoding="utf-8")


def ed(arquivo):
    return ("Edit", {"file_path": f"C:\\Users\\usuario\\proj\\comp\\{arquivo}", "old_string": "SEGREDO_x"}, False)


def sh(tool, comando, sub=False):
    return (tool, {"command": comando}, sub)


TURNOS = [
    [ed("a.py"), sh("Bash", "pytest -q"), ed("a.py")],                               # 0 codificação, 1 retry
    [ed("a.py"), sh("Bash", "pytest", sub=True), sh("Bash", "ls -la"), ed("a.py")],  # 1 codificação, 0 retry (subagente e leitura)
    [ed("a.py"), sh("Bash", "python x.py"), ed("b.py")],                             # 2 codificação, 0 retry (outro arquivo)
    [],                                                                              # 3 conversa
    [("Read", {"file_path": "C:\\Users\\usuario\\proj\\comp\\a.py"}, False), ("Grep", {"pattern": "x"}, False)],  # 4 exploração
    [sh("Bash", "python -m pytest")],                                                # 5 testes
    [sh("PowerShell", "git commit -m 'adiciona test'")],                             # 6 git
    [("Agent", {"prompt": "SEGREDO"}, False)],                                       # 7 delegação
    [("EnterPlanMode", {}, False)],                                                  # 8 planejamento
    [sh("Bash", "python x.py")],                                                     # 9 geral
    [sh("Bash", "rg padrao src/")],                                                  # 10 exploração
]


@pytest.fixture
def con(tmp_path):
    pasta = tmp_path / "logs" / "projects" / "proj"
    montar_log(pasta, TURNOS)
    c_ = db.conectar(tmp_path / "c.db")
    claude_code.ingerir(c_, pasta=tmp_path / "logs" / "projects")
    yield c_
    c_.close()


def test_turnos_e_categorias(con):
    turnos = cp.montar_turnos(con, ["sess-eeee-5555"])
    assert [t.categoria for t in turnos] == [
        "codificacao", "codificacao", "codificacao", "conversa", "exploracao", "testes", "git",
        "delegacao", "planejamento", "geral", "exploracao"]
    assert [t.retries for t in turnos[:3]] == [1, 0, 0]
    assert [t.indice for t in turnos] == list(range(11))


def test_subagente_nao_entra_na_sequencia_mas_o_custo_entra_no_turno(con):
    t1 = cp.montar_turnos(con, ["sess-eeee-5555"])[1]
    assert len(t1.chamadas) == 3                      # a tool do subagente (pytest) ficou de fora
    assert t1.chamadas_api == 4                       # mas a resposta dele custou neste turno
    assert t1.custo == pytest.approx(4 * CUSTO)


def test_resumo_da_sessao(con):
    r = cp.por_sessao(con, ["sess-eeee-5555"])["sess-eeee-5555"]
    assert r["categoria"] == "codificacao"            # 3 turnos, contra 2 de exploração
    assert (r["turnos"], r["turnos_edicao"], r["retries"]) == (11, 3, 1)
    assert r["one_shot"] == pytest.approx(2 / 3)


def test_por_categoria(con):
    linhas = {d["categoria"]: d for d in cp.por_categoria(cp.montar_turnos(con, ["sess-eeee-5555"]))}
    cod = linhas["codificacao"]
    assert (cod["turnos"], cod["turnos_edicao"], cod["retries"]) == (3, 3, 1)
    assert cod["one_shot"] == pytest.approx(2 / 3)
    assert cod["custo_usd"] == pytest.approx((3 + 4 + 3) * CUSTO)
    assert linhas["conversa"]["one_shot"] is None     # sem turno de edição: sem taxa, não 0%
    assert sum(d["fracao_turnos"] for d in linhas.values()) == pytest.approx(1)
    assert list(linhas)[0] == "codificacao"           # mais cara primeiro


def test_atividade_antes_do_primeiro_prompt_vira_um_turno_proprio(tmp_path):
    pasta = tmp_path / "logs" / "projects" / "proj"
    montar_log(pasta, [[]], antes=[[("Read", {"file_path": "C:\\x\\a.py"}, False)]])
    con = db.conectar(tmp_path / "p.db")
    claude_code.ingerir(con, pasta=tmp_path / "logs" / "projects")
    turnos = cp.montar_turnos(con, ["sess-eeee-5555"])
    assert [(t.indice, t.categoria) for t in turnos] == [(-1, "exploracao"), (0, "conversa")]


def test_sessao_sem_edicao_nao_tem_one_shot(tmp_path):
    pasta = tmp_path / "logs" / "projects" / "proj"
    montar_log(pasta, [[sh("Bash", "pytest")], []])
    con = db.conectar(tmp_path / "n.db")
    claude_code.ingerir(con, pasta=tmp_path / "logs" / "projects")
    r = cp.por_sessao(con, ["sess-eeee-5555"])["sess-eeee-5555"]
    assert r["one_shot"] is None and r["turnos_edicao"] == 0


def test_banco_nao_guarda_comando_nem_texto(con, tmp_path):
    con.commit()
    conteudo = (tmp_path / "c.db").read_bytes()
    # comandos completos: "pytest" sozinho aparece no caminho do arquivo de log (arquivos.caminho), que é esperado
    for texto in (b"SEGREDO", b"pytest -q", b"python -m pytest", b"git commit", b"rg padrao", b"adiciona test",
                  b"ls -la", b"python x.py"):
        assert texto not in conteudo
    classes = {r[0] for r in con.execute("SELECT DISTINCT subtipo FROM tool_calls WHERE subtipo IS NOT NULL")}
    assert classes == {"teste", "leitura", "outro", "git"}


def test_cli_sessoes_e_comportamento(tmp_path, monkeypatch, capsys):
    montar_log(tmp_path / "logs" / "projects" / "proj", TURNOS)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "logs"))
    banco = str(tmp_path / "cli.db")
    main(["--db", banco, "ingest"])
    capsys.readouterr()

    main(["--db", banco, "sessoes"])
    saida = capsys.readouterr().out
    assert "categoria" in saida and "one-shot" in saida and "Codificação" in saida and "67%" in saida

    main(["--db", banco, "sessoes", "--json"])
    (s,) = json.loads(capsys.readouterr().out)
    assert s["categoria"] == "codificacao" and s["turnos_edicao"] == 3 and s["retries"] == 1
    assert s["one_shot"] == pytest.approx(2 / 3)

    main(["--db", banco, "comportamento"])
    saida = capsys.readouterr().out
    assert "Codificação" in saida and "Exploração" in saida and "one-shot" in saida and "SEGREDO" not in saida

    main(["--db", banco, "comportamento", "--json"])
    dados = json.loads(capsys.readouterr().out)
    assert dados[0]["categoria"] == "codificacao" and isinstance(dados, list)

    main(["--db", banco, "comportamento", "--projeto", "nao-existe", "--json"])
    assert json.loads(capsys.readouterr().out) == []
