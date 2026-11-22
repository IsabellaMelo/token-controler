"""CLI do token-controler. Uso: `tc <comando>` ou `python -m token_controler <comando>`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import agora, comportamento, consultas, custo, db, detalhe, diagnostico, qualidade
from .formato import fmt_dur, fmt_pct, fmt_tokens, fmt_usd
from .fontes import claude_code


# ---------- formatação ----------

def tabela(cabecalho: list[str], linhas: list[list[str]], alinhar_esq: int = 1) -> str:
    if not linhas:
        return "(sem dados no período)"
    larg = [max(len(str(c)) for c in col) for col in zip(cabecalho, *linhas)]
    def fmt(l):
        return "  ".join(
            str(c).ljust(w) if i < alinhar_esq else str(c).rjust(w)
            for i, (c, w) in enumerate(zip(l, larg))
        )
    sep = "  ".join("-" * w for w in larg)
    return "\n".join([fmt(cabecalho), sep, *map(fmt, linhas)])


# ---------- filtros ----------

def filtro(args) -> consultas.Filtro:
    """O `Filtro` das consultas a partir das opções --desde/--ate/--hoje/--projeto/--ferramenta."""
    desde = "hoje" if getattr(args, "hoje", False) else getattr(args, "desde", None)
    return consultas.Filtro(
        desde=date.today().isoformat() if desde == "hoje" else desde,
        ate=getattr(args, "ate", None),
        projeto=getattr(args, "projeto", None),
        ferramenta=getattr(args, "ferramenta", None),
    )


def carregar_apelidos() -> dict[str, str]:
    arq = db.caminho_padrao().parent / "projetos.json"
    if arq.exists():
        return json.loads(arq.read_text(encoding="utf-8"))
    return {}


# ---------- comandos ----------

def cmd_ingest(con, args) -> None:
    mudou_modo = bool(args.modo) and args.modo != db.ler_modo(con)
    if mudou_modo:
        db.gravar_modo(con, args.modo)  # as chamadas novas já entram no modo escolhido
    r = claude_code.ingerir(con, tudo=args.tudo, apelidos=carregar_apelidos())
    if mudou_modo:
        # refaz o custo das chamadas antigas só com o que está no banco (não relê os logs)
        n = custo.recalcular(con, args.modo)
        con.commit()
        print(f"Modo de custo: {args.modo} ({n} chamada(s) recalculada(s))")
    if "aviso" in r:
        print(f"Claude Code: {r['aviso']}")
        return
    print(
        f"Claude Code: {r['arquivos_lidos']} arquivo(s) lido(s), {r['arquivos_pulados']} sem mudança, "
        f"{r['chamadas']} chamada(s), {r['tools']} tool call(s)"
        + (f", {r['linhas_ruins']} linha(s) ilegível(is)" if r["linhas_ruins"] else "")
    )


def rodape_custo(con, args) -> list[str]:
    """Linhas de rodapé do resumo: o modo de custo e de onde veio cada custo."""
    modo = db.ler_modo(con)
    origens = consultas.origens_do_custo(con, filtro(args))
    texto = ", ".join(f"{origens.get(o, 0)} {rot}" for o, rot in
                      (("registrado", "registrada(s)"), ("calculado", "calculada(s)"),
                       ("sem_preco", "sem preço")))
    saida = [f"Modo de custo: {modo} · chamadas: {texto}"]
    if origens.get("sem_preco"):
        saida.append(
            "* há chamadas sem custo: no modo display, as sem custo registrado no log"
            if modo == "display"
            else "* há chamadas com modelo fora da tabela de preços (veja `tc resumo --por modelo`)")
    return saida


def emitir_json(dados) -> None:
    """Imprime os dados como JSON (UTF-8, indentado). Só números e metadados, nunca texto de conversa."""
    print(json.dumps(dados, ensure_ascii=False, indent=2))


def linhas_resumo(con, args) -> list[dict]:
    """Resumo agrupado com números crus (consultas.resumo). None = não se aplica ou sem dado."""
    return consultas.resumo(con, filtro(args), args.por)


def _celula(v):
    """Valor ausente aparece como '-' na tabela."""
    return "-" if v is None else v


def dados_resumo(con, args) -> list[list[str]]:
    """Linhas formatadas do resumo, para a tabela."""
    saida = []
    for d in linhas_resumo(con, args):
        grupo = d[args.por]
        nome = grupo[:12] + "…" if args.por == "sessao" and len(grupo) > 13 else grupo
        saida.append([
            nome, d["sessoes"], _celula(d["prompts"]), d["chamadas"], _celula(d["tools"]),
            _celula(d["erros_tool"]), d["erros_api"], fmt_tokens(d["input"]), fmt_tokens(d["output"]),
            fmt_tokens(d["cache_write"]), fmt_tokens(d["cache_read"]), fmt_pct(d["cache_taxa"]),
            fmt_usd(d["custo_usd"]) + ("*" if d["chamadas_sem_preco"] else ""),
        ])
    return saida


CAB_RESUMO = ["{}", "sessões", "prompts", "chamadas", "tools", "erros tool", "erros api",
              "input", "output", "cache w", "cache r", "cache %", "custo"]


def cmd_resumo(con, args) -> None:
    if args.json:
        emitir_json(linhas_resumo(con, args))
        return
    linhas = dados_resumo(con, args)
    cab = [c.format(args.por) for c in CAB_RESUMO]
    print(tabela(cab, linhas))
    if linhas:
        print(f"\nCusto total estimado: {fmt_usd(consultas.custo_total(con, filtro(args)))}")
        for linha in rodape_custo(con, args):
            print(linha)


def linhas_sessoes(con, args) -> list[dict]:
    """Sessões com números crus (consultas.sessoes). Horário em UTC; durações em segundos inteiros."""
    return consultas.sessoes(con, filtro(args), args.limite)


def cmd_sessoes(con, args) -> None:
    sessoes = linhas_sessoes(con, args)
    if args.json:
        emitir_json(sessoes)
        return
    linhas = []
    for s in sessoes:
        inicio = datetime.fromisoformat(s["inicio"].replace("Z", "+00:00")).astimezone()
        linhas.append([
            s["sessao"][:8], s["projeto"], inicio.strftime("%d/%m %H:%M"),
            fmt_dur(s["duracao_s"]), fmt_dur(s["ativo_s"]), s["prompts"], s["chamadas"],
            s["tools"], s["erros_tool"], fmt_usd(s["custo_usd"]),
            comportamento.ROTULOS.get(s["categoria"], "-"), fmt_pct(s["one_shot"]),
        ])
    print(tabela(["sessão", "projeto", "início", "duração", "ativo", "prompts",
                  "chamadas", "tools", "erros tool", "custo", "categoria", "one-shot"], linhas, alinhar_esq=3))


def linhas_tools(con, args) -> list[dict]:
    """Uso por tool com números crus (consultas.tools); as recusas do usuário não contam como erro (T23)."""
    return consultas.tools(con, filtro(args))


def cmd_tools(con, args) -> None:
    tools = linhas_tools(con, args)
    if args.json:
        emitir_json(tools)
        return
    com_recusa = any(t["recusadas"] for t in tools)
    cab = ["tool", "chamadas", "erros", "taxa de erro"] + (["recusadas"] if com_recusa else [])
    linhas = [[t["tool"], t["chamadas"], t["erros"], fmt_pct(t["taxa_erro"])] + ([t["recusadas"]] if com_recusa else [])
              for t in tools]
    print(tabela(cab, linhas))
    por_cat: dict[str, dict[str, int]] = {}
    for t in tools:
        for cat, n in t["categorias"].items():
            por_cat.setdefault(cat, {})[t["tool"]] = n
    if por_cat:
        print("\nErros por categoria (as recusas do usuário ficam fora):")
        ordem = sorted(por_cat, key=lambda c: -sum(por_cat[c].values()))
        print(tabela(["categoria", "erros", "tools"], [
            [c, sum(por_cat[c].values()),
             ", ".join(f"{k} ({n})" for k, n in sorted(por_cat[c].items(), key=lambda x: -x[1])[:4])] for c in ordem]))
        if "sem_categoria" in por_cat:
            print("sem_categoria = erros lidos antes da T23: rode `tc ingest --tudo` para classificá-los.")
    if com_recusa:
        print("recusadas = tools que você negou na tela de permissão; não contam como erro.")


def linhas_comportamento(con, args) -> list[dict]:
    """Turnos por categoria (T14) com one-shot (T15) e custo (consultas.turnos_por_categoria)."""
    return consultas.turnos_por_categoria(con, filtro(args))


def cmd_comportamento(con, args) -> None:
    dados = linhas_comportamento(con, args)
    if args.json:
        emitir_json(dados)
        return
    linhas = [[
        comportamento.ROTULOS[d["categoria"]], d["turnos"], fmt_pct(d["fracao_turnos"]), d["turnos_edicao"],
        d["retries"], fmt_pct(d["one_shot"]), fmt_usd(d["custo_usd"]) + ("*" if d["chamadas_sem_custo"] else ""),
    ] for d in dados]
    print(tabela(["categoria", "turnos", "% turnos", "turnos de edição", "retries", "one-shot", "custo"], linhas))
    if dados:
        print(f"\nCusto total: {fmt_usd(sum(d['custo_usd'] for d in dados))}")
        print("Turno = um prompt e as tools até o próximo prompt. Categoria pelas tools usadas; só o Debug também usa um "
              "rótulo derivado do prompt (o texto nunca é gravado).\none-shot = turnos de edição sem retry; retry = o "
              "mesmo arquivo editado de novo depois de um comando de shell que não seja só de leitura.")
        if any(d["chamadas_sem_custo"] for d in dados):
            print("* há chamadas sem custo (modelo fora da tabela de preços, ou sem custo no modo atual)")


def cmd_diagnostico(con, args) -> None:
    limites, avisos = diagnostico.carregar_limites(diagnostico.caminho_config(args.db))
    for aviso in avisos:
        print(aviso, file=sys.stderr)
    if args.sessao:
        ids = diagnostico.resolver_sessao(con, args.sessao)
        if len(ids) != 1:
            motivo = "não encontrada" if not ids else f"ambígua ({len(ids)} sessões começam assim)"
            sys.exit(f"sessão '{args.sessao}' {motivo}")
    else:
        ids = consultas.ids_de_sessoes(con, filtro(args))
    todas = diagnostico.diagnosticar(con, limites, ids)
    recs = todas[:args.limite] if args.limite else todas
    if args.json:
        emitir_json(recs)
        return
    if not recs:
        print(f"Nenhum sinal acima dos limites em {len(ids)} sessão(ões).")
    for r in recs:
        print(f"[{r['severidade']}] {r['sessao'][:8]} · {r['projeto'] or '?'} · {diagnostico.ROTULOS[r['sinal']]}")
        print(f"    evidência: {r['evidencia']}")
        if r["impacto_usd"] is not None:
            print(f"    impacto estimado: {fmt_usd(r['impacto_usd'])}")
        print(f"    → {r['acao']}")
    if todas:
        print(f"\n{len(recs)} de {len(todas)} recomendação(ões), em {len({r['sessao'] for r in todas})} "
              f"sessão(ões). Ordem: severidade, depois impacto estimado em US$ (quando há conta), depois custo da sessão.")
    if not limites["janelas_contexto"]:
        print("Pressão de contexto: não avaliada (sem `janelas_contexto` no config.json; nenhuma tabela é presumida).")
    print("Os limites são propostas iniciais, ajustáveis em config.json (chave \"diagnostico\").")


def cmd_sessao(con, args) -> None:
    """Detalhe de uma sessão em árvore (T19). Sem ID, a sessão mais recente."""
    if args.json and args.comandos:
        sys.exit("--comandos só vale na tela: comandos de shell nunca saem em JSON.")
    limites, avisos = diagnostico.carregar_limites(diagnostico.caminho_config(args.db))
    for aviso in avisos:
        print(aviso, file=sys.stderr)
    if args.id:
        ids = diagnostico.resolver_sessao(con, args.id)
        if not ids:
            sys.exit(f"sessão '{args.id}' não encontrada. Rode `tc sessoes` para ver os IDs (use os primeiros caracteres).")
        if len(ids) > 1:
            lista = ", ".join(sorted(ids)[:5])
            sys.exit(f"sessão '{args.id}' ambígua ({len(ids)} sessões começam assim: {lista}...). Use mais caracteres.")
        sessao_id = ids[0]
    else:
        sessao_id = detalhe.ultima_sessao(con)
        if sessao_id is None:
            sys.exit("Nenhuma sessão no banco: rode `tc ingest` primeiro.")
    dados = detalhe.analisar(con, sessao_id, limites)
    if args.json:
        emitir_json(dados)
        return
    comandos = detalhe.comandos_da_sessao(con, sessao_id) if args.comandos else None
    visao = detalhe.montar(dados, comandos)
    try:
        from . import painel  # o rich só é carregado aqui; sem ele, a mesma árvore sai em texto simples
    except ImportError:
        print("O pacote rich não está instalado: mostrando a árvore em texto simples (`pip install -e .` instala o rich).",
              file=sys.stderr)
        print(detalhe.em_texto(visao))
        return
    painel.imprimir_sessao(visao)


def cmd_limpar_nomes(con, args) -> None:
    """Apaga os nomes de arquivo gravados em tool_calls.alvo_nome (T19, em teste)."""
    n = db.limpar_nomes(con)
    con.execute("VACUUM")  # sem isso, os nomes apagados continuariam nas páginas livres do arquivo do banco
    print(f"{n} nome(s) de arquivo apagado(s) do banco (coluna alvo_nome). O alvo_hash continua.")
    if db.gravar_nome_arquivo(con):
        print('Atenção: a gravação de nomes continua ligada. Para não gravar mais, coloque {"gravar_nome_arquivo": false} '
              "no config.json (ao lado do banco); senão o próximo `tc ingest` volta a gravar os nomes.")


def cmd_limpar_trechos(con, args) -> None:
    """Apaga os trechos de leitura gravados em tool_calls.leitura_ini/leitura_qtd (T19, em teste)."""
    n = db.limpar_trechos(con)
    con.execute("VACUUM")
    print(f"{n} trecho(s) de leitura apagado(s) do banco (colunas leitura_ini e leitura_qtd).")
    if db.gravar_trecho_leitura(con):
        print('Atenção: a gravação de trechos continua ligada. Para não gravar mais, coloque {"gravar_trecho_leitura": false} '
              "no config.json (ao lado do banco); senão o próximo `tc ingest` volta a gravar os trechos.")


def cmd_qualidade(con, args) -> None:
    dados = qualidade.coletar(con, args.ferramenta)
    if args.json:
        emitir_json(dados)
        return
    if not dados:
        print("(sem dados: rode `tc ingest`)")
        return
    linhas = [[
        q["ferramenta"], q["confianca"], q["arquivos"], _celula(q["linhas_ilegiveis"]), q["chamadas"],
        q["chamadas_sem_tokens"], q["chamadas_sem_preco"], q["origem_registrado"], q["origem_calculado"],
        q["origem_sem_preco"], "T09" if q["nivel_detalhe"] is None else q["nivel_detalhe"],
    ] for q in dados]
    print(f"Qualidade dos dados · modo de custo: {db.ler_modo(con)}\n")
    print(tabela(["ferramenta", "confiança", "arquivos", "linhas ilegíveis", "chamadas", "sem tokens",
                  "sem preço", "registrado", "calculado", "sem custo", "detalhe"], linhas))
    print()
    for q in dados:
        for motivo in q["motivos"]:
            print(f"- {q['ferramenta']} · confiança {q['confianca']}: {motivo}")
        if q["erros_tool"]:
            print(f"- {q['ferramenta']} · erros de tool sem categoria definida (outro): {q['erros_tool_outro']} de "
                  f"{q['erros_tool']} ({fmt_pct(q['erros_tool_outro'] / q['erros_tool'])}); quanto maior, mais regras faltam "
                  f"em regras_erros.py")
        if q["erros_tool_sem_categoria"]:
            print(f"- {q['ferramenta']} · {q['erros_tool_sem_categoria']} erro(s) lidos antes da T23, ainda sem categoria: "
                  f"rode `tc ingest --tudo`")
    print("Confiança: baixa = falta preço, tokens ou há linhas ilegíveis; media = só falta a contagem de "
          "linhas ilegíveis; alta = nenhum problema.\n"
          "'sem custo' = origem sem_preco no modo atual (no modo display inclui chamadas sem custo registrado).\n"
          "Nível de detalhe ('detalhe'): ainda não disponível, entra com a T09. Chamadas de erro de API "
          "ficam fora de 'sem tokens' e 'sem preço'.")


def cmd_painel(con, args) -> None:
    """Painel de período (rich): o antigo `tc live`. Sem o rich, cai na tabela simples de hoje."""
    if not args.simples:
        try:
            from . import painel  # o rich só é carregado aqui; os outros comandos não dependem dele
        except ImportError:
            print("O pacote rich não está instalado: mostrando o painel simples (`pip install -e .` instala o rich).",
                  file=sys.stderr)
            args.simples = True
    if not args.simples:
        limites, avisos = diagnostico.carregar_limites(diagnostico.caminho_config(args.db))
        for aviso in avisos:
            print(aviso, file=sys.stderr)
        try:
            painel.executar(con, limites, carregar_apelidos(), args.intervalo, args.periodo,
                            args.projeto, args.ferramenta)
        except KeyboardInterrupt:
            pass
        return
    args.por, args.hoje = "projeto", True
    apelidos = carregar_apelidos()
    try:
        while True:
            claude_code.ingerir(con, apelidos=apelidos)
            linhas = dados_resumo(con, args)
            total = consultas.custo_total(con, filtro(args))  # os mesmos filtros da tabela (hoje, --projeto...)
            sys.stdout.write("\033[2J\033[H")  # limpa a tela
            print(f"token-controler · hoje · atualizado {datetime.now():%H:%M:%S} "
                  f"(a cada {args.intervalo}s, Ctrl+C para sair)\n")
            print(tabela([c.format("projeto") for c in CAB_RESUMO], linhas))
            print(f"\nCusto estimado hoje: {fmt_usd(total)}")
            sys.stdout.flush()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print()


def cmd_web(con, args) -> None:
    """Painel local no navegador (T24). O servidor abre as próprias conexões (só leitura); esta aqui é fechada antes."""
    con.close()
    from .web import servidor  # só biblioteca padrão; carregado só aqui
    limites, avisos = diagnostico.carregar_limites(diagnostico.caminho_config(args.db))
    for aviso in avisos:
        print(aviso, file=sys.stderr)
    servidor.executar(args.db, args.porta, args.demo, limites, carregar_apelidos())


def cmd_live(con, args) -> None:
    """Tela "agora" (T20): sessões ativas, alertas, custo de hoje por projeto e por hora. Ctrl+C sai."""
    limites, avisos = diagnostico.carregar_limites(diagnostico.caminho_config(args.db))
    for aviso in avisos:
        print(aviso, file=sys.stderr)
    if not args.simples:
        try:
            from . import painel  # o rich só é carregado aqui; sem ele, a mesma tela sai em texto simples
        except ImportError:
            print("O pacote rich não está instalado: mostrando a tela em texto simples (`pip install -e .` instala o rich).",
                  file=sys.stderr)
            args.simples = True
    apelidos = carregar_apelidos()
    try:
        if not args.simples:
            painel.executar_agora(con, limites, apelidos, args.intervalo, args.projeto, args.ferramenta)
            return
        cache: dict = {}
        while True:
            claude_code.ingerir(con, apelidos=apelidos)
            dados = agora.coletar(con, agora.agora_utc(), limites, args.projeto, args.ferramenta, cache)
            sys.stdout.write("[2J[H")  # limpa a tela
            print(agora.em_texto(dados))
            sys.stdout.flush()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print()


# ---------- parser ----------

def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # acentos no terminal do Windows

    ap = argparse.ArgumentParser(prog="tc", description="Rastreia custo e uso das suas ferramentas de AI.")
    ap.add_argument("--db", type=Path, help="caminho do banco (padrão: ~/.token-controler/tracker.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def com_json(p):
        p.add_argument("--json", action="store_true", help="saída em JSON (lista de objetos, números sem formatação)")
        return p

    def com_filtros(p):
        p.add_argument("--desde", help="AAAA-MM-DD ou 'hoje'")
        p.add_argument("--ate", help="AAAA-MM-DD")
        p.add_argument("--hoje", action="store_true", help="atalho para --desde hoje")
        p.add_argument("--projeto")
        p.add_argument("--ferramenta")
        return p

    p = sub.add_parser("ingest", help="lê logs novos e grava no banco")
    p.add_argument("--tudo", action="store_true", help="reprocessa tudo (use após mudar preços)")
    p.add_argument("--modo", choices=custo.MODOS,
                   help="modo de custo: auto (registrado, senão calculado), calculate (sempre tokens x preço) "
                        "ou display (só o registrado). Fica gravado no banco; sem a opção, mantém o atual")
    p.set_defaults(func=cmd_ingest)

    p = com_json(com_filtros(sub.add_parser("resumo", help="tokens, chamadas, erros e custo agrupados")))
    p.add_argument("--por", choices=list(consultas.AGRUPAMENTOS), default="projeto")
    p.set_defaults(func=cmd_resumo)

    p = com_json(com_filtros(sub.add_parser("sessoes", help="lista sessões com duração e tempo ativo")))
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_sessoes)

    p = com_json(com_filtros(sub.add_parser("tools", help="uso e taxa de erro por tool")))
    p.set_defaults(func=cmd_tools)

    p = com_json(com_filtros(sub.add_parser("diagnostico", help="sinais de custo e lentidão, com evidência e recomendação")))
    p.add_argument("--sessao", help="só esta sessão (prefixo do ID, como em `tc sessoes`)")
    p.add_argument("--limite", type=int, default=20, help="máximo de recomendações (0 = todas); padrão 20")
    p.set_defaults(func=cmd_diagnostico)

    p = com_json(sub.add_parser("sessao", help="detalhe de uma sessão em árvore: prompts, tools, subagentes, pausas e ⚠"))
    p.add_argument("id", nargs="?", help="ID da sessão ou só o começo dele, como em `tc sessoes` (sem ID: a mais recente)")
    p.add_argument("--comandos", action="store_true",
                   help="mostra os comandos de shell, lidos agora do log original (em memória, nunca gravados)")
    p.set_defaults(func=cmd_sessao)

    p = sub.add_parser("limpar-nomes", help="apaga os nomes de arquivo gravados (coluna alvo_nome); o alvo_hash fica")
    p.set_defaults(func=cmd_limpar_nomes)

    p = sub.add_parser("limpar-trechos", help="apaga os trechos de leitura gravados (leitura_ini e leitura_qtd)")
    p.set_defaults(func=cmd_limpar_trechos)

    p = com_json(com_filtros(sub.add_parser("comportamento", help="categoria dos turnos, one-shot e custo por categoria")))
    p.set_defaults(func=cmd_comportamento)

    p = com_json(sub.add_parser("qualidade", help="confiança dos dados: linhas ilegíveis, modelos sem preço, sem tokens"))
    p.add_argument("--ferramenta")
    p.set_defaults(func=cmd_qualidade)

    p = sub.add_parser("live", help="tela focada em AGORA: sessões ativas, alertas, custo de hoje por projeto e por hora")
    p.add_argument("--intervalo", type=int, default=10, help="segundos entre atualizações")
    p.add_argument("--projeto")
    p.add_argument("--ferramenta")
    p.add_argument("--simples", action="store_true", help="texto puro, sem o rich")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("web", help="painel local no navegador (só em 127.0.0.1): tela Hoje, atualizada a cada 5 s")
    p.add_argument("--porta", type=int, help="porta (padrão: web_porta do config.json ou 8765; se ocupada, tenta as próximas)")
    p.add_argument("--demo", action="store_true",
                   help="dados fictícios e horário congelado, sem ingest nem recarregar (para olhar a tela e para os testes)")
    p.set_defaults(func=cmd_web)

    p = com_filtros(sub.add_parser("painel", help="painel de período que se atualiza sozinho (teclas 1-5 mudam o período, q sai)"))
    p.add_argument("--intervalo", type=int, default=15, help="segundos entre atualizações")
    p.add_argument("--periodo", choices=["hoje", "7d", "30d", "mes", "tudo"], default="hoje",
                   help="período inicial do painel (no painel, --desde e --ate não valem; use --periodo ou as teclas 1-5)")
    p.add_argument("--simples", action="store_true", help="painel antigo, só uma tabela de hoje (não usa o rich)")
    p.set_defaults(func=cmd_painel)

    args = ap.parse_args(argv)
    con = db.conectar(args.db)
    try:
        args.func(con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
