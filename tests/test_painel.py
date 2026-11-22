"""Painel do `tc live` (rich): coleta de números, desenho em várias larguras, teclas e laço principal."""

import io
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from rich.console import Console

from token_controler import cli, db, diagnostico, painel
from token_controler.fontes import claude_code

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code" / "projects"
HOJE = date(2026, 11, 21)                      # o dia dos logs sintéticos da sessão "rpps"
LIM = diagnostico.carregar_limites(None)[0]
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "p.db")
    claude_code.ingerir(c, pasta=FIXTURES)
    yield c
    c.close()


def texto(renderavel, largura=120, cor=False):
    """Desenha em memória. Sem cor, a saída é texto puro; com cor, traz os códigos ANSI."""
    buf = io.StringIO()
    Console(file=buf, width=largura, force_terminal=cor, color_system="truecolor" if cor else None).print(renderavel)
    return buf.getvalue()


# ---------- coleta ----------

@pytest.mark.parametrize("periodo, desde", [
    ("hoje", "2026-11-21"), ("7d", "2026-11-15"), ("30d", "2026-10-23"), ("mes", "2026-11-01"), ("tudo", None)])
def test_inicio_do_periodo(periodo, desde):
    assert painel.inicio_do_periodo(periodo, HOJE) == desde


def test_resumo_de_hoje_so_conta_o_dia(con):
    d = painel.coletar(con, "hoje", HOJE, LIM)
    r = d["resumo"]
    assert (r["chamadas"], r["sessoes"]) == (4, 1)                 # só a sessão rpps; clin11 é de 20/11
    assert r["custo"] == pytest.approx((106040 + 13200 + 35) / 1e6)
    assert (r["entrada"], r["saida"]) == (160, 550)
    assert r["cache_taxa"] == pytest.approx(20000 / (160 + 21000 + 20000))
    assert [p["projeto"] for p in d["projetos"]] == ["rpps"]


def test_periodo_tudo_inclui_os_outros_dias_e_conta_o_sem_preco(con):
    r = painel.coletar(con, "tudo", HOJE, LIM)["resumo"]
    assert (r["chamadas"], r["sessoes"], r["sem_preco"]) == (5, 2, 1)


def test_dias_ignoram_o_periodo_e_vem_do_mais_novo_ao_mais_antigo(con):
    d = painel.coletar(con, "hoje", HOJE, LIM)
    assert [x["dia"] for x in d["dias"]] == ["2026-11-21", "2026-11-20"]


def test_modelos_tools_shell_atividades_e_diagnostico(con):
    d = painel.coletar(con, "tudo", HOJE, LIM)
    assert {m["modelo"] for m in d["modelos"]} == {"claude-opus-5-5", "claude-haiku-5-5", "gpt-9-desconhecido"}
    assert "<synthetic>" not in {m["modelo"] for m in d["modelos"]}     # erro de API fora do painel de modelos
    assert sum(t["n"] for t in d["tools"]) == 3
    assert d["mcp"] == [] and d["shell"] == [] or isinstance(d["shell"], list)
    assert {a["categoria"] for a in d["atividades"]} <= set(painel.comportamento.ROTULOS)
    assert d["qualidade"][0]["ferramenta"] == "claude_code" and d["modo"] == "auto"


def test_filtros_de_projeto_e_ferramenta(con):
    d = painel.coletar(con, "tudo", HOJE, LIM, projeto="clin11")
    assert d["resumo"]["chamadas"] == 1 and [p["projeto"] for p in d["projetos"]] == ["clin11"]
    assert painel.coletar(con, "tudo", HOJE, LIM, projeto="nao-existe")["resumo"]["chamadas"] == 0
    assert painel.coletar(con, "tudo", HOJE, LIM, ferramenta="copilot")["resumo"]["chamadas"] == 0


def test_servidores_mcp_agrupam_pelo_nome(con):
    for i, nome in enumerate(("mcp__github__a", "mcp__github__b", "mcp__docs__x")):
        con.execute("INSERT INTO tool_calls (id, ferramenta, sessao_id, projeto, ts, tool, erro, subagente, arquivo) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (f"t{i}", "claude_code", "s", "p", "2026-11-21T12:00:00Z", nome, 0, 0, "f"))
    assert painel.coletar(con, "tudo", HOJE, LIM)["mcp"] == [("github", 2), ("docs", 1)]


def test_cache_reaproveita_o_trabalho_pesado_ate_o_banco_mudar(con, monkeypatch):
    chamadas = []
    original = painel.diagnostico.diagnosticar
    monkeypatch.setattr(painel.diagnostico, "diagnosticar", lambda *a, **k: chamadas.append(1) or original(*a, **k))
    cache = {}
    painel.coletar(con, "tudo", HOJE, LIM, cache=cache)
    painel.coletar(con, "tudo", HOJE, LIM, cache=cache)
    assert len(chamadas) == 1                                     # banco igual: não refez o diagnóstico
    con.execute("INSERT INTO eventos (ferramenta, sessao_id, projeto, ts, tipo, arquivo) "
                "VALUES ('claude_code','s','p','2026-11-21T13:00:00Z','atividade','f')")
    painel.coletar(con, "tudo", HOJE, LIM, cache=cache)
    assert len(chamadas) == 2                                     # o banco mudou: refez
    painel.coletar(con, "hoje", HOJE, LIM, cache=cache)
    assert len(chamadas) == 3                                     # outro período: refez


# ---------- desenho ----------

def test_barra_proporcional_e_com_largura_fixa():
    assert painel.barra(10, 10, 8).plain == "████████"
    assert painel.barra(5, 10, 8).plain == "████    "
    assert painel.barra(0, 10, 8).plain == " " * 8
    assert painel.barra(None, 0, 8).plain == " " * 8
    assert painel.barra(1, 10, 8).plain == "▊       "            # 0,8 de um bloco = 6 oitavos
    assert all(len(painel.barra(v, 7, 10).plain) == 10 for v in (0, 1, 3.3, 7))


def test_painel_traz_todas_as_caixas_e_o_resumo(con):
    saida = texto(painel.renderizar(painel.coletar(con, "tudo", HOJE, LIM), 140, datetime(2026, 11, 21, 12, 0, 0), 15))
    for titulo in ("token-controler", "Por dia", "Por projeto", "Por atividade", "Por modelo", "Diagnóstico",
                   "Tools mais usadas", "Shell (por classe)", "MCP", "Qualidade dos dados"):
        assert titulo in saida, titulo
    assert "[ Tudo ]" in saida and "[1-5] período" in saida and "[q] sair" in saida
    assert "5 chamadas" in saida and "2 sessões" in saida and "* 1 chamada(s) sem preço" in saida
    assert "rpps" in saida and "clin11" in saida and "opus-5-5" in saida and "sem uso de MCP" in saida
    assert "atualizado 12:00:00 · a cada 15s" in saida


def test_aba_do_periodo_ativo_muda(con):
    for chave, nome in painel.PERIODOS:
        saida = texto(painel.renderizar(painel.coletar(con, chave, HOJE, LIM), 140))
        assert f"[ {nome} ]" in saida
        assert sum(f"[ {n} ]" in saida for _, n in painel.PERIODOS) == 1


@pytest.mark.parametrize("largura, colunas", [(158, 3), (110, 3), (109, 2), (72, 2), (71, 1), (50, 1)])
def test_layout_se_adapta_a_largura(con, largura, colunas):
    saida = texto(painel.renderizar(painel.coletar(con, "tudo", HOJE, LIM), largura), largura)
    linhas = saida.splitlines()
    assert all(len(l) <= largura for l in linhas)                 # nada passa da largura do terminal
    assert any("[ Tudo ]" in l for l in linhas)                   # a aba ativa nunca quebra no meio, nem em tela estreita
    topo = next(l for l in linhas if "Por dia" in l)
    assert len(re.findall(r"[┌╭]", topo)) == colunas              # caixas lado a lado na primeira fileira


def test_caixas_da_mesma_fileira_tem_a_mesma_altura(con):
    linhas = texto(painel.renderizar(painel.coletar(con, "tudo", HOJE, LIM), 140), 140).splitlines()
    inicio = next(i for i, l in enumerate(linhas) if "Por dia" in l)
    fim = next(i for i, l in enumerate(linhas) if i > inicio and l.lstrip().startswith(("└", "╰")))
    assert linhas[fim].count("└") + linhas[fim].count("╰") == 3   # as 3 caixas terminam na mesma linha


def test_painel_sem_dados_nao_quebra(tmp_path):
    vazio = db.conectar(tmp_path / "vazio.db")
    saida = texto(painel.renderizar(painel.coletar(vazio, "hoje", HOJE, LIM), 120))
    assert "0 chamadas" in saida and "sem dados" in saida and "nenhum sinal no período" in saida
    assert "sem uso de MCP" in saida and "sem uso de shell" in saida


def test_com_cor_traz_codigos_ansi_e_sem_cor_nao(con):
    renderavel = painel.renderizar(painel.coletar(con, "tudo", HOJE, LIM), 120)
    assert "\x1b[" in texto(renderavel, 120, cor=True)
    assert "\x1b[" not in texto(renderavel, 120, cor=False)
    assert ANSI.sub("", texto(renderavel, 120, cor=True)).count("Por dia") == 1


def test_painel_nao_exibe_texto_de_conversa(con):
    saida = texto(painel.renderizar(painel.coletar(con, "tudo", HOJE, LIM), 140))
    for proibido in ("SEGREDO", "analise os fundos", "gera relatorio", "e agora?"):
        assert proibido not in saida


def test_diagnostico_aparece_com_severidade(con):
    for i in range(3):
        con.execute("INSERT INTO tool_calls (id, ferramenta, sessao_id, projeto, ts, tool, erro, subagente, arquivo) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (f"f{i}", "claude_code", "sess-aaaa-1111", "rpps",
                                                   f"2026-11-21T12:3{i}:00Z", "Bash", 1, 0, "f"))
    saida = texto(painel.renderizar(painel.coletar(con, "tudo", HOJE, LIM), 140))
    assert "● sess-aaa loop de retry" in saida


# ---------- teclado ----------

class FakeMsvcrt:
    def __init__(self, teclas):
        self.teclas = list(teclas)

    def kbhit(self):
        return bool(self.teclas)

    def getwch(self):
        return self.teclas.pop(0)


class TTY(io.StringIO):
    def isatty(self):
        return True


def test_esperar_tecla_sem_terminal_so_dorme(monkeypatch):
    dormiu = []
    monkeypatch.setattr(painel.time, "sleep", dormiu.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert painel.esperar_tecla(4) is None and dormiu == [4]


def test_esperar_tecla_no_windows(monkeypatch):
    monkeypatch.setattr(sys, "stdin", TTY())
    monkeypatch.setattr(painel, "msvcrt", FakeMsvcrt(["3"]))
    assert painel.esperar_tecla(1) == "3"
    monkeypatch.setattr(painel, "msvcrt", FakeMsvcrt(["\xe0", "H"]))      # seta para cima: 2 códigos, descartados
    assert painel.esperar_tecla(1) is None
    monkeypatch.setattr(painel, "msvcrt", FakeMsvcrt([]))
    monkeypatch.setattr(painel.time, "sleep", lambda s: None)
    relogio = iter([0.0, 0.0, 99.0, 99.0])
    monkeypatch.setattr(painel.time, "monotonic", lambda: next(relogio))
    assert painel.esperar_tecla(1) is None                                # passou o tempo sem tecla


# ---------- laço principal ----------

def teclas(monkeypatch, sequencia):
    restantes = list(sequencia)
    monkeypatch.setattr(painel, "esperar_tecla", lambda timeout: restantes.pop(0))
    return restantes


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    monkeypatch.setattr(painel, "date", type("D", (date,), {"today": classmethod(lambda cls: HOJE)}))
    c = db.conectar(tmp_path / "loop.db")
    yield c
    c.close()


def rodar(con, **kw):
    buf = io.StringIO()
    painel.executar(con, LIM, {}, intervalo=1, console=Console(file=buf, width=120, force_terminal=False), **kw)
    return buf.getvalue()


def test_laco_desenha_troca_de_periodo_com_as_teclas_e_sai_com_q(ambiente, monkeypatch):
    restantes = teclas(monkeypatch, [None, "3", "x", "5", "Q"])
    saida = rodar(ambiente)
    abas = re.findall(r"\[ (Hoje|7 dias|30 dias|Mês|Tudo) \]", saida)
    assert abas == ["Hoje", "Hoje", "30 dias", "30 dias", "Tudo"]         # tecla inválida ("x") mantém o período
    assert restantes == []


def test_laco_em_terminal_usa_a_tela_alternativa_e_restaura_ao_sair(ambiente, monkeypatch):
    teclas(monkeypatch, ["2", "q"])
    buf = io.StringIO()
    console = Console(file=buf, width=120, height=40, force_terminal=True, color_system="truecolor", legacy_windows=False)
    painel.executar(ambiente, LIM, {}, intervalo=1, console=console)
    saida = buf.getvalue()
    assert saida.index("\x1b[?1049h") < saida.index("Por projeto") < saida.rindex("\x1b[?1049l")   # entra, desenha, sai
    assert "\x1b[?25h" in saida                                                                    # cursor de volta
    assert "7 dias" in ANSI.sub("", saida)                                                         # a tecla 2 trocou a aba


@pytest.mark.parametrize("tecla", ["q", "\x03", "\x1b"])
def test_laco_sai_com_q_ctrl_c_ou_esc(ambiente, monkeypatch, tecla):
    teclas(monkeypatch, [tecla])
    assert saida_unica(rodar(ambiente)) == 1


def saida_unica(saida):
    return saida.count("Por projeto")


def test_laco_comeca_no_periodo_pedido_e_respeita_o_projeto(ambiente, monkeypatch):
    teclas(monkeypatch, ["q"])
    saida = rodar(ambiente, periodo="tudo", projeto="clin11")
    assert "[ Tudo ]" in saida and "clin11" in saida and "rpps" not in saida


def test_laco_le_os_logs_a_cada_quadro(ambiente, monkeypatch):
    teclas(monkeypatch, [None, "q"])
    lidos = []
    original = claude_code.ingerir
    monkeypatch.setattr(painel.claude_code, "ingerir", lambda *a, **k: lidos.append(1) or original(*a, **k))
    rodar(ambiente)
    assert len(lidos) == 2


# ---------- CLI ----------

def test_cli_painel_padrao_usa_o_painel_novo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    teclas(monkeypatch, ["q"])
    cli.main(["--db", str(tmp_path / "c.db"), "painel", "--periodo", "tudo", "--intervalo", "1"])
    saida = capsys.readouterr().out
    assert "Por projeto" in saida and "[ Tudo ]" in saida and "rpps" in saida


def test_cli_painel_sai_limpo_com_ctrl_c(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))

    def interrompe(timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(painel, "esperar_tecla", interrompe)
    cli.main(["--db", str(tmp_path / "c.db"), "painel"])                  # não pode levantar
    assert "Por projeto" in capsys.readouterr().out


def test_cli_painel_sem_rich_cai_no_painel_simples(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(FIXTURES.parent))
    import token_controler
    monkeypatch.delattr(token_controler, "painel")                      # o import em cache no pacote...
    monkeypatch.setitem(sys.modules, "token_controler.painel", None)    # ...e no sys.modules: agora falha como sem rich
    monkeypatch.setattr(cli.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    cli.main(["--db", str(tmp_path / "c.db"), "painel"])
    capturado = capsys.readouterr()
    assert "rich não está instalado" in capturado.err
    assert "token-controler · hoje · atualizado" in capturado.out        # o painel antigo
