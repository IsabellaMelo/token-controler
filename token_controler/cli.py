"""CLI do token-controler. Uso: `tc <comando>` ou `python -m token_controler <comando>`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import comportamento, custo, db, diagnostico, qualidade
from .formato import fmt_dur, fmt_pct, fmt_tokens, fmt_usd
from .fontes import claude_code

PAUSA_MAX_S = 300  # pausas acima de 5 min não contam como tempo ativo

AGRUPAMENTOS = {
    "projeto": "COALESCE(projeto, '?')",
    "dia": "date(ts, 'localtime')",
    "sessao": "sessao_id",
    "ferramenta": "ferramenta",
    "modelo": "COALESCE(modelo, '?')",
}


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

def filtros(args) -> tuple[str, list]:
    cond, params = [], []
    desde = "hoje" if getattr(args, "hoje", False) else getattr(args, "desde", None)
    if desde:
        cond.append("date(ts, 'localtime') >= ?")
        params.append(date.today().isoformat() if desde == "hoje" else desde)
    if getattr(args, "ate", None):
        cond.append("date(ts, 'localtime') <= ?")
        params.append(args.ate)
    if getattr(args, "projeto", None):
        cond.append("projeto = ?")
        params.append(args.projeto)
    if getattr(args, "ferramenta", None):
        cond.append("ferramenta = ?")
        params.append(args.ferramenta)
    return (" AND ".join(cond) or "1=1"), params


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
    where, p = filtros(args)
    origens = dict(con.execute(
        f"SELECT origem_custo, COUNT(*) FROM chamadas WHERE origem_custo IS NOT NULL AND {where} "
        "GROUP BY origem_custo", p).fetchall())
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
    """Resumo agrupado com números crus. None = não se aplica ou sem dado (a tabela mostra '-')."""
    g = AGRUPAMENTOS[args.por]
    where, p = filtros(args)
    linhas: dict[str, dict] = {}

    for r in con.execute(f"""
        SELECT {g} AS grupo, COUNT(DISTINCT sessao_id) AS sessoes, COUNT(*) AS chamadas,
               SUM(erro_api) AS erros_api, SUM(input_tokens) AS inp, SUM(output_tokens) AS outp,
               SUM(cache_write_5m + cache_write_1h) AS cw, SUM(cache_read) AS cr,
               SUM(custo_usd) AS custo, SUM(custo_usd IS NULL AND erro_api = 0) AS sem_preco
        FROM chamadas WHERE {where} GROUP BY grupo""", p):
        linhas[r["grupo"]] = dict(r)

    if args.por != "modelo":
        for r in con.execute(f"""
            SELECT {g} AS grupo, COUNT(*) AS n FROM eventos
            WHERE tipo = 'prompt' AND {where} GROUP BY grupo""", p):
            linhas.setdefault(r["grupo"], {"grupo": r["grupo"]})["prompts"] = r["n"]
        for r in con.execute(f"""
            SELECT {g} AS grupo, COUNT(*) AS n, SUM(COALESCE(erro, 0)) AS erros FROM tool_calls
            WHERE {where} GROUP BY grupo""", p):
            d = linhas.setdefault(r["grupo"], {"grupo": r["grupo"]})
            d["tools"], d["erros_tool"] = r["n"], r["erros"]

    saida = []
    por_modelo = args.por == "modelo"
    for grupo in sorted(linhas, key=lambda k: -(linhas[k].get("custo") or 0)):
        d = linhas[grupo]
        entrada, cw, cr = d.get("inp") or 0, d.get("cw") or 0, d.get("cr") or 0
        total_in = entrada + cw + cr
        saida.append({
            args.por: str(grupo),
            "sessoes": d.get("sessoes") or 0,
            "prompts": None if por_modelo else d.get("prompts"),
            "chamadas": d.get("chamadas") or 0,
            "tools": None if por_modelo else d.get("tools"),
            "erros_tool": None if por_modelo else d.get("erros_tool"),
            "erros_api": d.get("erros_api") or 0,
            "input": entrada,
            "output": d.get("outp") or 0,
            "cache_write": cw,
            "cache_read": cr,
            "cache_taxa": cr / total_in if total_in else None,  # fração de 0 a 1
            "custo_usd": d.get("custo"),                        # None = nenhuma chamada com preço
            "chamadas_sem_preco": d.get("sem_preco") or 0,      # a tabela marca com '*'
        })
    return saida


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
        total = con.execute(f"SELECT SUM(custo_usd) FROM chamadas WHERE {filtros(args)[0]}",
                            filtros(args)[1]).fetchone()[0]
        print(f"\nCusto total estimado: {fmt_usd(total)}")
        for linha in rodape_custo(con, args):
            print(linha)


def linhas_sessoes(con, args) -> list[dict]:
    """Sessões com números crus. Horário em UTC (ISO 8601); durações em segundos inteiros."""
    where, p = filtros(args)
    sql = f"""
    WITH ev AS (
        SELECT sessao_id, projeto, ts,
               (julianday(ts) - julianday(LAG(ts) OVER (PARTITION BY sessao_id ORDER BY ts))) * 86400 AS gap
        FROM eventos WHERE tipo = 'atividade' AND {where}
    ), s AS (
        SELECT sessao_id, MAX(projeto) AS projeto, MIN(ts) AS inicio, MAX(ts) AS fim,
               SUM(CASE WHEN gap <= {PAUSA_MAX_S} THEN gap ELSE 0 END) AS ativo
        FROM ev GROUP BY sessao_id
    )
    SELECT s.*,
        (julianday(s.fim) - julianday(s.inicio)) * 86400 AS duracao,
        (SELECT COUNT(*) FROM eventos e WHERE e.sessao_id = s.sessao_id AND e.tipo = 'prompt') AS prompts,
        (SELECT COUNT(*) FROM chamadas c WHERE c.sessao_id = s.sessao_id) AS chamadas,
        (SELECT COUNT(*) FROM tool_calls t WHERE t.sessao_id = s.sessao_id) AS tools,
        (SELECT SUM(COALESCE(erro,0)) FROM tool_calls t WHERE t.sessao_id = s.sessao_id) AS erros,
        (SELECT SUM(custo_usd) FROM chamadas c WHERE c.sessao_id = s.sessao_id) AS custo
    FROM s ORDER BY inicio DESC LIMIT ?"""
    linhas = [
        {
            "sessao": r["sessao_id"],
            "projeto": r["projeto"] or "?",
            "inicio": r["inicio"],
            # julianday() devolve ruído de ponto flutuante (29.9999982 s): arredonda, não trunca
            "duracao_s": round(r["duracao"] or 0),
            "ativo_s": round(r["ativo"] or 0),
            "prompts": r["prompts"],
            "chamadas": r["chamadas"],
            "tools": r["tools"],
            "erros_tool": r["erros"] or 0,
            "custo_usd": r["custo"],
        }
        for r in con.execute(sql, [*p, args.limite])
    ]
    por_sessao = comportamento.por_sessao(con, [l["sessao"] for l in linhas])
    for l in linhas:  # categoria (T14) e one-shot (T15), calculados do que já está no banco
        c = por_sessao.get(l["sessao"], {})
        l["categoria"] = c.get("categoria")
        l["turnos_edicao"] = c.get("turnos_edicao", 0)
        l["retries"] = c.get("retries", 0)
        l["one_shot"] = c.get("one_shot")  # fração de 0 a 1; None se a sessão não teve edição
    return linhas


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
    """Uso por tool com números crus. taxa_erro é uma fração de 0 a 1."""
    where, p = filtros(args)
    return [
        {
            "tool": r["tool"],
            "chamadas": r["n"],
            "erros": r["erros"],
            "taxa_erro": r["erros"] / r["n"] if r["n"] else None,
        }
        for r in con.execute(f"""
            SELECT tool, COUNT(*) AS n, SUM(COALESCE(erro,0)) AS erros FROM tool_calls
            WHERE {where} GROUP BY tool ORDER BY n DESC""", p)
    ]


def cmd_tools(con, args) -> None:
    tools = linhas_tools(con, args)
    if args.json:
        emitir_json(tools)
        return
    linhas = [[t["tool"], t["chamadas"], t["erros"], fmt_pct(t["taxa_erro"])] for t in tools]
    print(tabela(["tool", "chamadas", "erros", "taxa de erro"], linhas))


def linhas_comportamento(con, args) -> list[dict]:
    """Turnos por categoria (T14) com one-shot (T15) e custo, das sessões com atividade no filtro."""
    where, p = filtros(args)
    sessoes = [r[0] for r in con.execute(f"SELECT DISTINCT sessao_id FROM eventos WHERE {where}", p)]
    return comportamento.por_categoria(comportamento.montar_turnos(con, sessoes))


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
        where, p = filtros(args)
        ids = [r[0] for r in con.execute(f"SELECT DISTINCT sessao_id FROM eventos WHERE {where}", p)]
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
    print("Confiança: baixa = falta preço, tokens ou há linhas ilegíveis; media = só falta a contagem de "
          "linhas ilegíveis; alta = nenhum problema.\n"
          "'sem custo' = origem sem_preco no modo atual (no modo display inclui chamadas sem custo registrado).\n"
          "Nível de detalhe ('detalhe'): ainda não disponível, entra com a T09. Chamadas de erro de API "
          "ficam fora de 'sem tokens' e 'sem preço'.")


def cmd_live(con, args) -> None:
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
            where, p = filtros(args)  # os mesmos filtros da tabela (hoje, --projeto, --ferramenta...)
            total = con.execute(f"SELECT SUM(custo_usd) FROM chamadas WHERE {where}", p).fetchone()[0]
            sys.stdout.write("\033[2J\033[H")  # limpa a tela
            print(f"token-controler · hoje · atualizado {datetime.now():%H:%M:%S} "
                  f"(a cada {args.intervalo}s, Ctrl+C para sair)\n")
            print(tabela([c.format("projeto") for c in CAB_RESUMO], linhas))
            print(f"\nCusto estimado hoje: {fmt_usd(total)}")
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
    p.add_argument("--por", choices=list(AGRUPAMENTOS), default="projeto")
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

    p = com_json(com_filtros(sub.add_parser("comportamento", help="categoria dos turnos, one-shot e custo por categoria")))
    p.set_defaults(func=cmd_comportamento)

    p = com_json(sub.add_parser("qualidade", help="confiança dos dados: linhas ilegíveis, modelos sem preço, sem tokens"))
    p.add_argument("--ferramenta")
    p.set_defaults(func=cmd_qualidade)

    p = com_filtros(sub.add_parser("live", help="painel que se atualiza sozinho (teclas 1-5 mudam o período, q sai)"))
    p.add_argument("--intervalo", type=int, default=15, help="segundos entre atualizações")
    p.add_argument("--periodo", choices=["hoje", "7d", "30d", "mes", "tudo"], default="hoje",
                   help="período inicial do painel (no painel, --desde e --ate não valem; use --periodo ou as teclas 1-5)")
    p.add_argument("--simples", action="store_true", help="painel antigo, só uma tabela de hoje (não usa o rich)")
    p.set_defaults(func=cmd_live)

    args = ap.parse_args(argv)
    con = db.conectar(args.db)
    try:
        args.func(con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
