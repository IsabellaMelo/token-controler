"""T19, etapa 2: `tc sessao` (árvore de uma sessão). Fixture sintética em tests/fixtures/claude_code_sessao.

A sessão `sess-eeee-5555` (UTC, 21/11/2026) tem 3 prompts, 25 chamadas e 22 tools:
  prompt 1 (10:00): Read analise.py (tA1), RELEITURA DESNECESSÁRIA (tA2), Edit (tE1), releitura LEGÍTIMA depois do Edit (tA3),
                    duas tools na mesma chamada (tT1 = tests\\analise.py, tU1 = util.py), 3 falhas de Bash (tB1..tB3), 3 Edit com
                    erro em alvos diferentes (tF1, tF2 em analise.py, tF3 em util.py), Agent (tAg) com um subagente.
  pausa de 9min20s
  prompt 2 (10:12): Read analise.py (tA4, RELEITURA DESNECESSÁRIA, cache regravado após a pausa), re-criação de cache sem pausa
                    (48,5k gravados e nenhum lido, 25 s depois), Bash git status (tB4, com 12k de conteúdo novo e o cache lido:
                    NÃO é re-criação).
  compactação às 10:14
  prompt 3 (10:15): Read analise.py (tA5, releitura LEGÍTIMA: houve compactação), cache regravado após a compactação (esperado);
                    quatro Read de dados.py por trechos: linhas 1-100 (tP1), 101-200 (tP2, outro trecho: normal),
                    150-249 (tP3, repete 150-200) e arquivo inteiro (tP4: repete só ~12% das 2000 linhas que o Read lê, abaixo do limiar de 25%).
"""

import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from token_controler import cli, db, detalhe, diagnostico, painel
from token_controler.fontes import claude_code
from token_controler.precos import calcular_custo

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_sessao" / "projects"
SESSAO = "sess-eeee-5555"
LIM = diagnostico.carregar_limites(None)[0]


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "s.db")
    claude_code.ingerir(c, pasta=FIXTURES)
    yield c
    c.close()


@pytest.fixture
def dados(con):
    return detalhe.analisar(con, SESSAO, LIM)


def tools_da_arvore(dados):
    """Todos os nós de tool (inclusive os dos subagentes), por id curto."""
    achados = {}

    def visitar(it):
        if it["tipo"] == "tool":
            achados[it["id"].split(":")[1]] = it
            for f in it["filhos"]:
                visitar(f)
        elif it["tipo"] == "subagente":
            for f in it["filhos"]:
                visitar(f)

    for t in dados["turnos"]:
        for it in t["itens"]:
            visitar(it)
    return achados


def problemas(dados, tipo):
    return [p for p in dados["problemas"] if p["tipo"] == tipo]


def custo_da_chamada(con, msg, **uso):
    r = con.execute("SELECT * FROM chamadas WHERE id = ?", (f"claude_code:{msg}:{uso.pop('req')}",)).fetchone()
    return r["custo_usd"]


# ---------- cabeçalho e estrutura ----------

def test_cabecalho(dados, con):
    assert dados["sessao"] == SESSAO and dados["projeto"] == "arvore"
    assert (dados["prompts"], dados["chamadas"], dados["tools"], dados["erros_tool"], dados["compactacoes"]) == (3, 25, 22, 6, 1)
    assert dados["duracao_s"] == 920                                  # 10:00:00 a 10:15:20
    assert dados["custo_usd"] == pytest.approx(con.execute("SELECT SUM(custo_usd) FROM chamadas").fetchone()[0])
    assert dados["categoria"] == "exploracao"


def test_tres_galhos_de_prompt_e_o_custo_dos_galhos_fecha_com_o_total(dados):
    assert [t["indice"] for t in dados["turnos"]] == [0, 1, 2]
    assert sum(t["custo_usd"] for t in dados["turnos"]) == pytest.approx(dados["custo_usd"])
    assert [t["tools"] for t in dados["turnos"]] == [15, 2, 5]


def test_subagente_aninhado_dentro_da_chamada_agent(dados):
    agent = tools_da_arvore(dados)["tAg"]
    assert [f["tipo"] for f in agent["filhos"]] == ["subagente"]
    sub = agent["filhos"][0]
    assert [f["tool"] for f in sub["filhos"]] == ["Read", "Grep"] and sub["chamadas"] == 3
    assert sub["custo_usd"] > 0
    # o subagente não aparece solto nem repetido no turno
    assert all(it["tipo"] != "subagente" for t in dados["turnos"] for it in t["itens"])


def test_custo_da_tool_e_o_da_chamada_que_a_pediu(dados, con):
    custo_m2 = calcular_custo("claude-opus-5-5", 100, 50, 500, 0, 20000)
    assert tools_da_arvore(dados)["tA2"]["custo_usd"] == pytest.approx(custo_m2)


def test_tools_pedidas_na_mesma_chamada_dividem_o_custo(dados):
    t = tools_da_arvore(dados)
    custo_m5 = calcular_custo("claude-opus-5-5", 100, 60, 400, 0, 21000)
    assert t["tT1"]["custo_usd"] == t["tU1"]["custo_usd"] == pytest.approx(custo_m5 / 2)


def test_nome_duracao_e_resultado_de_cada_tool(dados):
    t = tools_da_arvore(dados)
    assert (t["tA1"]["alvo_nome"], t["tA1"]["erro"], t["tA1"]["duracao_ms"]) == ("analise.py", 0, 1000)
    assert t["tB1"]["erro"] == 1 and t["tB1"]["subtipo"] == "teste" and t["tB1"]["alvo_nome"] is None
    assert t["tU1"]["alvo_nome"] == "util.py"
    assert t["tAg"]["espera"] is True and t["tA1"]["espera"] is False


def test_pausa_entre_prompts(dados):
    assert [t["pausa_antes_s"] for t in dados["turnos"]] == [None, pytest.approx(560), pytest.approx(59)]


# ---------- ⚠ releitura ----------

def test_releitura_desnecessaria_e_a_legitima(dados):
    t = tools_da_arvore(dados)
    marcadas = {i for i, it in t.items() if any("releitura" in m for m in it["marcas"])}
    assert marcadas == {"tA2", "tA4", "tP3"}                        # tP4 (arquivo inteiro) repete só 12% de um arquivo de 2000 linhas
    assert t["tA1"]["marcas"] == []                                 # primeira leitura
    assert t["tA3"]["marcas"] == []                                 # depois de um Edit com sucesso: normal
    assert t["tA5"]["marcas"] == []                                 # depois de uma compactação: normal
    assert t["tT1"]["marcas"] == []                                 # mesmo nome, outra pasta: outro arquivo
    assert t["tS1"]["marcas"] == []                                 # o subagente tem o próprio contexto
    assert t["tP1"]["marcas"] == [] and t["tP2"]["marcas"] == []    # trechos diferentes do mesmo arquivo: não é releitura


def test_edit_com_erro_nao_justifica_reler(dados):
    """tF1 e tF2 (Edit de analise.py) falharam: o arquivo não mudou, então a leitura tA4 continua sendo releitura."""
    assert any("releitura" in m for m in tools_da_arvore(dados)["tA4"]["marcas"])


def test_problema_de_releitura_tem_motivo_e_custo(dados):
    por_arquivo = {p["motivo"].split(":")[0]: p for p in problemas(dados, "releitura")}
    assert set(por_arquivo) == {"analise.py", "dados.py"}
    t = tools_da_arvore(dados)
    p = por_arquivo["analise.py"]
    assert "2 leitura(s) repetindo linhas já lidas" in p["motivo"] and p["dados"] == {"releituras": 2, "certa": True}
    assert p["custo_usd"] == pytest.approx(t["tA2"]["custo_usd"] + t["tA4"]["custo_usd"])
    assert por_arquivo["dados.py"]["custo_usd"] == pytest.approx(t["tP3"]["custo_usd"])


def test_leitura_depois_de_edicao_em_outra_thread_tambem_e_normal(con):
    """Um subagente editou o arquivo entre duas leituras da thread principal: não é releitura desnecessária."""
    h = con.execute("SELECT alvo_hash FROM tool_calls WHERE id = 'claude_code:tA1'").fetchone()[0]
    con.execute("DELETE FROM tool_calls WHERE id IN ('claude_code:tE1')")           # tira o Edit da thread principal
    con.execute("""UPDATE tool_calls SET tool = 'Edit', erro = 0 WHERE id = 'claude_code:tS1'""")   # o subagente passa a editar
    con.execute("UPDATE tool_calls SET alvo_hash = ? WHERE id = 'claude_code:tS1'", (h,))
    con.execute("UPDATE tool_calls SET ts = '2026-11-21T10:00:15.000Z' WHERE id = 'claude_code:tS1'")   # entre tA2 e tA3
    d = detalhe.analisar(con, SESSAO, LIM)
    t = tools_da_arvore(d)
    assert not any("releitura" in m for m in t["tA3"]["marcas"])


# ---------- ⚠ falha repetida ----------

def test_falha_repetida_exige_a_mesma_tool_no_mesmo_alvo(dados):
    (p,) = problemas(dados, "falha_repetida")
    assert p["dados"] == {"tool": "Bash", "vezes": 3} and p["severidade"] == "media"
    t = tools_da_arvore(dados)
    assert [t[i]["marcas"] for i in ("tB1", "tB2", "tB3")] == [["falha repetida 1/3"], ["falha repetida 2/3"], ["falha repetida 3/3"]]
    assert p["custo_usd"] == pytest.approx(sum(t[i]["custo_usd"] for i in ("tB1", "tB2", "tB3")))
    # 3 Edit com erro seguidos, mas em 2 alvos diferentes (analise.py, analise.py, util.py): não é a mesma tool no mesmo alvo
    assert all(t[i]["marcas"] == [] for i in ("tF1", "tF2", "tF3"))


def test_regra_do_diagnostico_continua_por_tool_e_o_detalhe_reaproveita_a_funcao(con):
    s = diagnostico.carregar_sessoes(con, [SESSAO])[0]
    r = {x["sinal"]: x for x in diagnostico.diagnosticar(con, LIM, [SESSAO])}
    assert r["loop_retry"]["dados"]["maior_sequencia"] == 3        # no diagnóstico, Bash x3 e Edit x3 contam por tool
    principais = [t for t in s["tools"] if not t["subagente"]]
    assert [c["tamanho"] for c in diagnostico.corridas_de_falhas(principais, LIM)] == [3, 3]
    assert [c["tamanho"] for c in diagnostico.corridas_de_falhas(principais, LIM, por_alvo=True)] == [3]


def test_limite_do_diagnostico_vale_para_a_falha_repetida(con):
    d = detalhe.analisar(con, SESSAO, {**LIM, "loop_min": 4})
    assert problemas(d, "falha_repetida") == []
    d = detalhe.analisar(con, SESSAO, {**LIM, "loop_alta": 3})
    assert problemas(d, "falha_repetida")[0]["severidade"] == "alta"


# ---------- ⚠ pausa, cache ----------

def test_pausa_longa_diz_o_cache_regravado_e_o_custo(dados):
    (p,) = problemas(dados, "pausa_longa")
    assert p["dados"]["segundos"] == pytest.approx(560) and p["dados"]["entre_prompts"] is True
    assert p["dados"]["tokens_regravados"] == 48000 and p["dados"]["regravou"] is True
    esperado = (48000 * (5.00 - 0.20)) / 1e6                          # opus-5-5: gravar 5 US$/M, ler 0,20 US$/M
    assert p["custo_usd"] == pytest.approx(esperado)
    assert dados["turnos"][1]["pausa_problema"] is p or dados["turnos"][1]["pausa_problema"] == p


def test_pausa_dentro_do_limite_nao_e_problema(con):
    d = detalhe.analisar(con, SESSAO, {**LIM, "pausa_s": 700})
    assert problemas(d, "pausa_longa") == []


def test_recriacao_de_cache_so_fora_de_pausa_e_fora_de_compactacao(dados):
    (p,) = problemas(dados, "recriacao_cache")
    assert p["dados"]["tokens_regravados"] == 48500                  # 25 s depois da chamada anterior, sem ler o cache
    assert p["custo_usd"] == pytest.approx(48500 * (5.00 - 0.20) / 1e6)
    # os 48k depois da pausa estão na pausa; os 60k depois da compactação são esperados; os 12k de conteúdo novo
    # (cache lido normalmente) são só uma leitura grande: nenhum deles vira re-criação
    assert [x["dados"]["tokens_regravados"] for x in problemas(dados, "recriacao_cache")] == [48500]
    assert any(it["tipo"] == "recriacao" for it in dados["turnos"][1]["itens"])


def test_cache_baixo_usa_a_regra_e_o_limite_do_diagnostico(dados, con):
    (p,) = problemas(dados, "cache_baixo")
    rec = diagnostico.diagnosticar(con, LIM, [SESSAO])
    assert p["dados"]["chamadas"] == 25
    assert p["dados"]["taxa"] == pytest.approx(dados["cache_taxa"])
    assert p["motivo"].startswith(next(r for r in rec if r["sinal"] == "cache_baixo")["evidencia"])
    assert p["custo_usd"] > 0


def test_compactacao_aparece_como_no_da_arvore(dados):
    assert any(it["tipo"] == "compactacao" for it in dados["turnos"][1]["itens"])


def test_problemas_vem_ordenados_por_gravidade(dados):
    ordem = {"alta": 0, "media": 1, "baixa": 2}
    sev = [ordem[p["severidade"]] for p in dados["problemas"]]
    assert sev == sorted(sev)
    assert {p["tipo"] for p in dados["problemas"]} == {"cache_baixo", "pausa_longa", "recriacao_cache", "falha_repetida", "releitura"}


# ---------- desenho ----------

def test_texto_tem_a_arvore_os_problemas_e_as_sugestoes(dados):
    texto = detalhe.em_texto(detalhe.montar(dados))
    for esperado in ("Sessão sess-eee · arvore", "custo", "categoria", "Linha do tempo", "Prompt 1", "Prompt 3", "subagente",
                     "analise.py", "util.py", "⚠", "⏸ pausa de 9min20s", "contexto compactado", "Problemas (6)",
                     "Falha repetida", "Releitura desnecessária", "Pausa longa", "Cache baixo", "Para custar menos", "→"):
        assert esperado in texto, esperado
    assert "├──" in texto and "└──" in texto


def test_sem_o_flag_nenhum_comando_de_shell_aparece(dados):
    texto = detalhe.em_texto(detalhe.montar(dados))
    assert "SEGREDO" not in texto and "pytest" not in texto and "git status" not in texto
    assert "[teste]" in texto                                       # só a classe do comando


def test_com_comandos_le_do_log_original_e_nao_grava_nada(dados, con, tmp_path):
    comandos = detalhe.comandos_da_sessao(con, SESSAO)
    assert set(comandos) == {"claude_code:tB1", "claude_code:tB2", "claude_code:tB3", "claude_code:tB4"}
    texto = detalhe.em_texto(detalhe.montar(dados, comandos))
    assert "$ SEGREDO_comando pytest -q tests" in texto and "$ git status SEGREDO_comando" in texto
    con.commit()
    assert b"SEGREDO_comando" not in (tmp_path / "s.db").read_bytes()
    assert con.execute("SELECT COUNT(*) FROM tool_calls WHERE subtipo = 'teste'").fetchone()[0] == 3   # nada novo no banco


def test_comando_longo_e_cortado_em_uma_linha(dados):
    assert detalhe._uma_linha("a  b\n  c") == "a b c"
    cortado = detalhe._uma_linha("x" * 500)
    assert len(cortado) == detalhe.COMANDO_MAX and cortado.endswith("…")


def test_log_apagado_mostra_que_o_comando_nao_esta_disponivel(tmp_path):
    copia = tmp_path / "logs"
    shutil.copytree(FIXTURES, copia)
    c = db.conectar(tmp_path / "x.db")
    claude_code.ingerir(c, pasta=copia)
    shutil.rmtree(copia)
    comandos = detalhe.comandos_da_sessao(c, SESSAO)
    assert comandos == {}
    texto = detalhe.em_texto(detalhe.montar(detalhe.analisar(c, SESSAO, LIM), comandos))
    assert "comando indisponível" in texto and "SEGREDO" not in texto
    c.close()


def test_sessao_ingerida_antes_do_nome_mostra_o_hash_curto(con):
    con.execute("UPDATE tool_calls SET alvo_nome = NULL")
    d = detalhe.analisar(con, SESSAO, LIM)
    assert d["tools_com_hash_sem_nome"] == 16
    texto = detalhe.em_texto(detalhe.montar(d))
    h = con.execute("SELECT alvo_hash FROM tool_calls WHERE id = 'claude_code:tA1'").fetchone()[0][:8]
    assert f"#{h}" in texto and "analise.py" not in texto and "util.py" not in texto
    assert "tc ingest --tudo" in texto                              # a nota explica como preencher


def test_tools_sem_hash_nem_nome_ficam_sem_alvo(con):
    con.execute("UPDATE tool_calls SET alvo_nome = NULL, alvo_hash = NULL")
    texto = detalhe.em_texto(detalhe.montar(detalhe.analisar(con, SESSAO, LIM)))
    assert "#" not in texto.split("Problemas")[0].replace("# ", "")  # nenhum hash na árvore
    assert "Linha do tempo" in texto


def render_rich(visao, largura=140):
    buf = io.StringIO()
    Console(file=buf, width=largura, force_terminal=False, color_system=None).print(painel.renderizar_sessao(visao))
    return buf.getvalue()


def test_rich_tree_desenha_a_mesma_arvore(dados):
    saida = render_rich(detalhe.montar(dados))
    for esperado in ("Linha do tempo", "Prompt 1", "analise.py", "⚠", "subagente", "Problemas (6)", "Para custar menos"):
        assert esperado in saida, esperado
    assert "├──" in saida and "└──" in saida


def test_rich_nao_interpreta_colchetes_do_nome_do_arquivo():
    visao = detalhe.Visao(cabecalho=[[("topo", "titulo")]], raiz=detalhe.No([("raiz", "titulo")]))
    visao.raiz.add(("Read  ", "tool"), ("[red]x[/red].py", "alvo"))
    assert "[red]x[/red].py" in render_rich(visao)


# ---------- comando ----------

@pytest.fixture
def banco(tmp_path):
    caminho = tmp_path / "cli.db"
    c = db.conectar(caminho)
    claude_code.ingerir(c, pasta=FIXTURES)
    c.close()
    return caminho


def rodar(banco, *args):
    cli.main(["--db", str(banco), "sessao", *args])


def test_comando_aceita_o_prefixo_do_id(banco, capsys):
    rodar(banco, "sess-ee")
    saida = capsys.readouterr().out
    assert "Linha do tempo" in saida and "Prompt 3" in saida and "⚠" in saida


def test_sem_id_usa_a_ultima_sessao(banco, capsys):
    c = db.conectar(banco)
    c.execute("""INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo)
                 VALUES ('claude_code', 'sess-zzzz-9999', 'outro', '2026-12-01T00:00:00Z', 'atividade', 'f')""")
    c.commit()
    c.close()
    rodar(banco)
    assert "Sessão sess-zzz · outro" in capsys.readouterr().out
    assert detalhe.ultima_sessao(db.conectar(banco)) == "sess-zzzz-9999"


def test_sessao_inexistente_diz_claramente(banco):
    with pytest.raises(SystemExit) as e:
        rodar(banco, "nao-existe")
    assert "não encontrada" in str(e.value) and "tc sessoes" in str(e.value)


def test_prefixo_ambiguo_lista_as_candidatas(banco):
    c = db.conectar(banco)
    c.execute("""INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo)
                 VALUES ('claude_code', 'sess-eeee-6666', 'outro', '2026-11-21T09:00:00Z', 'atividade', 'f')""")
    c.commit()
    c.close()
    with pytest.raises(SystemExit) as e:
        rodar(banco, "sess-eeee")
    assert "ambígua" in str(e.value) and "sess-eeee-5555" in str(e.value) and "sess-eeee-6666" in str(e.value)
    rodar(banco, "sess-eeee-5")                                       # prefixo maior resolve


def test_banco_vazio_sem_id(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(tmp_path / "vazio.db"), "sessao"])
    assert "Nenhuma sessão" in str(e.value)


def test_sessao_sem_tools_diz_isso(tmp_path, capsys):
    caminho = tmp_path / "so_conversa.db"
    c = db.conectar(caminho)
    c.execute("""INSERT INTO chamadas (id, ferramenta, sessao_id, projeto, ts, modelo, input_tokens, output_tokens,
                 custo_usd, arquivo) VALUES ('claude_code:m:r', 'claude_code', 'sess-conversa', 'p', '2026-11-21T10:00:00Z',
                 'claude-opus-5-5', 10, 10, 0.01, 'f')""")
    c.execute("""INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo)
                 VALUES ('claude_code', 'sess-conversa', 'p', '2026-11-21T10:00:00Z', 'prompt', 'f')""")
    c.commit()
    c.close()
    cli.main(["--db", str(caminho), "sessao", "sess-conv"])
    saida = capsys.readouterr().out
    assert "não tem tool calls" in saida and "Prompt 1" in saida


def test_json_tem_numeros_e_nenhum_comando(banco, capsys):
    rodar(banco, "sess-ee", "--json")
    dados = json.loads(capsys.readouterr().out)
    assert dados["sessao"] == SESSAO and len(dados["turnos"]) == 3 and len(dados["problemas"]) == 6
    assert "SEGREDO" not in json.dumps(dados)


def test_comandos_nao_combina_com_json(banco):
    with pytest.raises(SystemExit) as e:
        rodar(banco, "--json", "--comandos")
    assert "--comandos" in str(e.value)


def test_comandos_na_tela(banco, capsys):
    rodar(banco, "sess-ee", "--comandos")
    assert "SEGREDO_comando pytest -q tests" in capsys.readouterr().out


def test_sem_rich_cai_no_texto_simples(banco, capsys, monkeypatch):
    import builtins
    original = builtins.__import__

    def sem_painel(nome, globais=None, locais=None, fromlist=(), nivel=0):
        if nivel == 1 and "painel" in (fromlist or ()):
            raise ImportError("sem rich")
        return original(nome, globais, locais, fromlist, nivel)

    monkeypatch.setattr(builtins, "__import__", sem_painel)
    rodar(banco, "sess-ee")
    captura = capsys.readouterr()
    assert "rich não está instalado" in captura.err
    assert "Linha do tempo" in captura.out and "├──" in captura.out


def test_o_comando_nao_mexe_no_banco(banco):
    antes = banco.read_bytes()
    rodar(banco, "sess-ee")
    c = db.conectar(banco)
    c.close()
    assert banco.read_bytes() == antes


# ---------- regra de re-criação compartilhada com o diagnóstico ----------

def test_recriacoes_de_cache_separa_pausa_compactacao_e_prefixo_perdido(con):
    s = diagnostico.carregar_sessoes(con, [SESSAO])[0]
    compactacoes = [r[0] for r in con.execute("SELECT ts FROM eventos WHERE tipo = 'compactacao'")]
    itens = {r["chamada"]["id"].split(":")[1]: r for r in diagnostico.recriacoes_de_cache(s["chamadas"], LIM, compactacoes)}
    assert set(itens) == {"m14", "m15", "m16", "m17"}                # tudo que gravou mais de 10k fora a primeira chamada
    assert (itens["m14"]["apos_pausa"], itens["m14"]["prefixo_perdido"]) == (True, True)
    assert (itens["m15"]["apos_pausa"], itens["m15"]["prefixo_perdido"]) == (False, True)
    assert (itens["m16"]["apos_pausa"], itens["m16"]["prefixo_perdido"]) == (False, False)     # só conteúdo novo
    assert itens["m17"]["apos_compactacao"] is True and itens["m16"]["apos_compactacao"] is False


def test_o_diagnostico_continua_contando_so_as_recriacoes_apos_pausa(con):
    r = {x["sinal"]: x for x in diagnostico.diagnosticar(con, LIM, [SESSAO])}
    assert r["recriacao_cache"]["dados"]["recriacoes"] == 1 and r["recriacao_cache"]["dados"]["tokens_regravados"] == 48000


def test_limite_da_perda_de_cache_e_configuravel(con):
    d = detalhe.analisar(con, SESSAO, {**LIM, "recriacao_perda_pct": 0.0})      # nenhuma leitura "cai" abaixo de 0
    assert problemas(d, "recriacao_cache") == []


# ---------- pausa longa só é ⚠ quando o cache foi regravado ----------

def linha_com(texto, trecho):
    return next(l for l in texto.splitlines() if trecho in l)


def test_pausa_sem_regravar_o_cache_nao_e_problema_nem_tem_aviso(con):
    """Cache de 1 h: a pausa de 9min20s não expirou nada, a chamada seguinte só leu o cache."""
    con.execute("UPDATE chamadas SET cache_write_5m = 100, cache_read = 48000 WHERE id = 'claude_code:m14:r14'")
    d = detalhe.analisar(con, SESSAO, LIM)
    assert problemas(d, "pausa_longa") == []
    assert d["turnos"][1]["pausa_problema"] is None and d["turnos"][1]["pausa_info"]["regravou"] is False
    texto = detalhe.em_texto(detalhe.montar(d))
    linha = linha_com(texto, "⏸ pausa de 9min20s")
    assert "⚠" not in linha and "cache não regravado (100 tokens gravados)" in linha
    assert "Pausa longa" not in texto                                  # nem na lista de problemas, nem nas sugestões


def test_pausa_que_regravou_o_cache_continua_com_aviso(dados):
    linha = linha_com(detalhe.em_texto(detalhe.montar(dados)), "⏸ pausa de 9min20s")
    assert "⚠ cache expirado, 48,000 tokens regravados" in linha and "a mais que ler do cache" in linha


def test_pausa_no_meio_do_prompt_segue_a_mesma_regra(con):
    con.execute("UPDATE chamadas SET cache_write_5m = 100, cache_read = 48000 WHERE id = 'claude_code:m14:r14'")
    # a pausa de 9min20s passa a ser "dentro" do prompt 1: o prompt 2 começa só depois da primeira chamada
    con.execute("UPDATE eventos SET ts = '2026-11-21T10:12:07.000Z' WHERE tipo = 'prompt' AND ts = '2026-11-21T10:12:00.000Z'")
    con.execute("UPDATE eventos SET ts = '2026-11-21T10:12:07.000Z' WHERE tipo = 'atividade' AND ts = '2026-11-21T10:12:00.000Z'")
    d = detalhe.analisar(con, SESSAO, LIM)
    pausas = [it for t in d["turnos"] for it in t["itens"] if it["tipo"] == "pausa"]
    assert pausas and all(p["problema"] is None for p in pausas)
    assert "⚠" not in linha_com(detalhe.em_texto(detalhe.montar(d)), "⏸ pausa de")
