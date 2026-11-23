"""T19, etapa 1: só o NOME do arquivo (sem pasta) vai para `tool_calls.alvo_nome`; nunca o caminho, o comando ou o conteúdo."""

import json
import re
import sqlite3
from pathlib import Path

import pytest

from token_controler import alvo, cli, db
from token_controler.fontes import claude_code

RAIZ = Path(__file__).parent / "fixtures"
TOOLS = RAIZ / "claude_code_tools" / "projects"
SESSAO = RAIZ / "claude_code_sessao" / "projects"


def ingerido(tmp_path, pasta=TOOLS, nome="t.db"):
    con = db.conectar(tmp_path / nome)
    claude_code.ingerir(con, pasta=pasta)
    return con


def nomes(con):
    return {r["id"].split(":")[1]: r["alvo_nome"] for r in con.execute("SELECT id, alvo_nome FROM tool_calls")}


# ---------- a função ----------

@pytest.mark.parametrize("caminho, esperado", [
    ("C:\\Users\\usuario\\proj\\analise.py", "analise.py"),
    ("/home/usuario/proj/analise.py", "analise.py"),
    ("c:/users/usuario/proj/analise.py/", "analise.py"),            # barra final
    ("pasta\\sub/misto.py", "misto.py"),                         # barras misturadas
    ("analise.py", "analise.py"),                                # já é só o nome
    ("C:analise.py", "analise.py"),                              # unidade sem barra
    ("\\\\servidor\\compartilhada\\relatorio final.xlsx", "relatorio final.xlsx"),
])
def test_nome_do_arquivo_remove_a_pasta(caminho, esperado):
    assert alvo.nome_do_arquivo(caminho) == esperado


@pytest.mark.parametrize("valor", [None, 123, ["a.py"], "", "   ", "/", "\\\\", "C:\\", "C:"])
def test_nome_do_arquivo_vazio_ou_invalido_vira_none(valor):
    assert alvo.nome_do_arquivo(valor) is None


def test_nome_do_arquivo_nunca_tem_separador_nem_passa_do_limite():
    entradas = ["C:\\a\\b\\c.py", "/a/b/c.py", "a\\b/c\\d.py", "x" * 500 + ".py", "pasta\\" + "y" * 300,
                "C:\\a\\" + "z" * 120 + ".ipynb", "a\x00b\x1f.py", "com\nquebra.py"]
    for e in entradas:
        nome = alvo.nome_do_arquivo(e)
        assert nome is not None
        assert "/" not in nome and "\\" not in nome, e
        assert len(nome) <= alvo.NOME_MAX, e
        assert nome.isprintable(), e


def test_nome_longo_mantem_a_extensao():
    nome = alvo.nome_do_arquivo("C:\\p\\" + "n" * 200 + ".py")
    assert nome.endswith(".py") and len(nome) == alvo.NOME_MAX


def test_nome_da_tool_so_para_tools_com_arquivo():
    assert alvo.nome_da_tool({"file_path": "C:\\a\\b.py"}) == "b.py"
    assert alvo.nome_da_tool({"notebook_path": "/a/n.ipynb"}) == "n.ipynb"
    assert alvo.nome_da_tool({"command": "ls C:\\a\\b.py"}) is None     # shell: comando é texto livre
    assert alvo.nome_da_tool({"path": "C:\\a"}) is None                  # Grep/Glob: pasta de busca
    assert alvo.nome_da_tool("texto") is None and alvo.nome_da_tool(None) is None


# ---------- o que vai para o banco ----------

def test_grava_so_o_nome_e_mantem_o_hash_do_caminho(tmp_path):
    con = ingerido(tmp_path)
    n = nomes(con)
    assert (n["tA1"], n["tA2"]) == ("SEGREDO_a.py", "segredo_a.py")     # o nome fica como foi escrito; o hash ignora a caixa
    assert (n["tE"], n["tL"], n["tM"]) == ("SEGREDO_b.py", "SEGREDO_c.py", "SEGREDO_n.ipynb")
    assert n["tB"] is None and n["tZ"] is None and n["tX"] is None      # Bash, Grep e file_path que não é texto
    hashes = {r["id"]: r["alvo_hash"] for r in con.execute("SELECT id, alvo_hash FROM tool_calls")}
    assert re.fullmatch(r"[0-9a-f]{16}", hashes["claude_code:tA1"])
    con.close()


def test_valor_gravado_nunca_tem_separador_de_pasta(tmp_path):
    for pasta in (TOOLS, SESSAO):
        con = ingerido(tmp_path, pasta, nome=pasta.parent.name + ".db")
        gravados = [r[0] for r in con.execute("SELECT alvo_nome FROM tool_calls WHERE alvo_nome IS NOT NULL")]
        assert gravados
        assert all("/" not in v and "\\" not in v and len(v) <= alvo.NOME_MAX for v in gravados)
        con.close()


def test_banco_nao_tem_nenhum_caminho_completo_nem_comando_nem_conteudo(tmp_path):
    for pasta in (TOOLS, SESSAO):
        con = ingerido(tmp_path, pasta, nome=pasta.parent.name + ".db")
        con.commit()
        conteudo = (tmp_path / (pasta.parent.name + ".db")).read_bytes()
        con.close()
        for proibido in (b"proj\\tools", b"proj/tools", b"proj\\arvore", b"proj/arvore", b"usuario\\proj", b"usuario/proj",
                         b"echo ", b"pytest", b"git status", b"SEGREDO_comando", b"SEGREDO_prompt", b"SEGREDO_resultado",
                         b"SEGREDO_resumo", b"SEGREDO_resposta", b"SEGREDO_tarefa", b"SEGREDO_agente", b"SEGREDO_padrao"):
            assert proibido not in conteudo, proibido


def test_mesmo_nome_em_pastas_diferentes_se_distingue_pelo_hash(tmp_path):
    con = ingerido(tmp_path, SESSAO)
    linhas = {r["id"]: r for r in con.execute("SELECT * FROM tool_calls WHERE id IN ('claude_code:tA1', 'claude_code:tT1')")}
    a, t = linhas["claude_code:tA1"], linhas["claude_code:tT1"]       # src\analise.py e tests\analise.py
    assert a["alvo_nome"] == t["alvo_nome"] == "analise.py"
    assert a["alvo_hash"] != t["alvo_hash"]
    con.close()


# ---------- chave de configuração ----------

def configurar(tmp_path, conteudo):
    (tmp_path / "config.json").write_text(conteudo if isinstance(conteudo, str) else json.dumps(conteudo), encoding="utf-8")


def test_padrao_e_gravar(tmp_path):
    con = db.conectar(tmp_path / "t.db")
    assert db.gravar_nome_arquivo(con) is True
    con.close()


@pytest.mark.parametrize("conteudo", [{"gravar_nome_arquivo": True}, {"diagnostico": {}}, {"gravar_nome_arquivo": "nao"},
                                      {"gravar_nome_arquivo": 0}, "{ isso não é json"])
def test_config_que_nao_desliga_continua_gravando(tmp_path, conteudo):
    configurar(tmp_path, conteudo)
    con = ingerido(tmp_path)
    assert nomes(con)["tA1"] == "SEGREDO_a.py"
    con.close()


def test_chave_false_desliga_so_o_nome_e_o_hash_continua(tmp_path):
    configurar(tmp_path, {"gravar_nome_arquivo": False})
    con = ingerido(tmp_path)
    assert db.gravar_nome_arquivo(con) is False
    assert set(nomes(con).values()) == {None}
    assert con.execute("SELECT COUNT(*) FROM tool_calls WHERE alvo_hash IS NOT NULL").fetchone()[0] == 6   # os mesmos que teriam nome
    con.close()


def test_chave_false_nao_atrapalha_o_diagnostico(tmp_path):
    from token_controler import diagnostico
    configurar(tmp_path, {"gravar_nome_arquivo": False, "diagnostico": {"pausa_s": 120}})
    limites, avisos = diagnostico.carregar_limites(tmp_path / "config.json")
    assert limites["pausa_s"] == 120 and avisos == []


# ---------- migração ----------

def test_banco_antigo_ganha_a_coluna_alvo_nome_com_null(tmp_path):
    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.executescript("""
        CREATE TABLE tool_calls (id TEXT PRIMARY KEY, ferramenta TEXT NOT NULL, sessao_id TEXT NOT NULL,
            projeto TEXT, ts TEXT NOT NULL, tool TEXT NOT NULL, erro INTEGER,
            subagente INTEGER NOT NULL DEFAULT 0, arquivo TEXT NOT NULL, duracao_ms INTEGER, alvo_hash TEXT, subtipo TEXT);
        INSERT INTO tool_calls VALUES ('claude_code:x', 'claude_code', 's', 'p', '2026-11-01T00:00:00Z', 'Read', 0, 0, 'f',
                                       10, 'abcd', NULL);
    """)
    antigo.commit()
    antigo.close()
    c = db.conectar(caminho)
    assert c.execute("SELECT alvo_nome FROM tool_calls").fetchone()[0] is None
    db.conectar(caminho).close()                      # rodar a migração de novo não quebra
    c.close()


# ---------- tc limpar-nomes ----------

def test_limpar_nomes_apaga_os_nomes_do_banco_e_do_arquivo(tmp_path, capsys):
    caminho = tmp_path / "t.db"
    con = ingerido(tmp_path)
    con.commit()
    con.close()
    assert b"SEGREDO_a.py" in caminho.read_bytes()

    cli.main(["--db", str(caminho), "limpar-nomes"])
    saida = capsys.readouterr().out
    assert "6 nome(s) de arquivo apagado(s)" in saida
    assert "gravar_nome_arquivo" in saida                       # avisa que a gravação continua ligada

    con = db.conectar(caminho)
    assert con.execute("SELECT COUNT(*) FROM tool_calls WHERE alvo_nome IS NOT NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM tool_calls WHERE alvo_hash IS NOT NULL").fetchone()[0] == 6   # o hash fica
    con.close()
    conteudo = caminho.read_bytes()
    assert b"SEGREDO_a.py" not in conteudo and b"SEGREDO_b.py" not in conteudo     # nem nas páginas livres (VACUUM)


def test_limpar_nomes_com_a_gravacao_desligada_nao_avisa(tmp_path, capsys):
    configurar(tmp_path, {"gravar_nome_arquivo": False})
    cli.main(["--db", str(tmp_path / "t.db"), "limpar-nomes"])
    saida = capsys.readouterr().out
    assert "0 nome(s)" in saida and "Atenção" not in saida


def test_reingerir_depois_de_limpar_com_a_chave_ligada_volta_a_gravar(tmp_path):
    con = ingerido(tmp_path)
    db.limpar_nomes(con)
    assert set(nomes(con).values()) == {None}
    claude_code.ingerir(con, pasta=TOOLS, tudo=True)
    assert nomes(con)["tA1"] == "SEGREDO_a.py"
    con.close()


# ---------- compactação (marcador para a releitura) ----------

def test_compactacao_vira_evento_so_com_o_horario_e_o_resumo_nao_conta_como_prompt(tmp_path):
    con = ingerido(tmp_path, SESSAO)
    assert con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'compactacao'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'prompt'").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'compactacao' AND rotulo IS NOT NULL").fetchone()[0] == 0
    con.close()
