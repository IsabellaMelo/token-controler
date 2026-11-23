"""Telas do `tc web`: transformam os dicts de `consultas.py` em HTML, a partir dos modelos em `web/modelos/`.

Regra de segurança: todo valor entra no modelo escapado por `html.escape`, a não ser que seja um `Html` (um trecho
montado aqui mesmo, a partir de outros valores já escapados). Assim, um nome de projeto ou de tool com `<`, `"` ou `&`
nunca vira marcação. Nenhum texto de conversa chega aqui: só o que está no banco (números, nomes de projeto, de modelo e
de tool, rótulos fixos).
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from pathlib import Path
from string import Template
from urllib.parse import quote, urlencode

from .. import diagnostico

MODELOS = Path(__file__).parent / "modelos"
DIAS_DA_SEMANA = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
PERIODOS = (("hoje", "Hoje"), ("7d", "7 dias"), ("30d", "30 dias"), ("mes", "Mês"), ("tudo", "Tudo"))
DIAS_ABREVIADOS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
COLUNAS_DE_HORA = 12
COLUNAS_SEM_VALOR = 14       # com mais barras que isso, o valor sai de cima da barra (fica no title)
ALTURA_MAX_PX = 170          # a barra da hora mais cara
ALTURA_MIN_PX = 4
TAXA_ERRO_DESTAQUE = 0.02    # tool com 2% de erro ou mais fica em vermelho
NOMES_DOS_TIPOS = {"output": "Output", "cache_write": "Gravação de cache", "cache_read": "Leitura de cache", "input": "Input"}
CORES_DOS_TIPOS = {"output": "#F5A04A", "cache_write": "#6AA6FF", "cache_read": "#3567B8", "input": "#D3DAE4"}
CLASSES_DOS_TIPOS = {"output": "cor-output", "cache_write": "cor-gravacao", "cache_read": "cor-leitura", "input": "cor-input"}


# ---------- modelos e escape ----------

class Html(str):
    """Trecho de HTML já montado com valores escapados: entra no modelo como está."""


def _valor(v) -> str:
    if isinstance(v, Html):
        return v
    return html.escape("" if v is None else str(v), quote=True)


def _ler(nome: str) -> str:
    return (MODELOS / nome).read_text(encoding="utf-8")


def _fragmentos() -> dict[str, Template]:
    partes = re.split(r"^<!-- @(\w+) -->\n", _ler("fragmentos.html"), flags=re.M)
    return {nome: Template(corpo.rstrip("\n")) for nome, corpo in zip(partes[1::2], partes[2::2])}


FRAGMENTOS = _fragmentos()
BASE = Template(_ler("base.html"))
HOJE = Template(_ler("hoje.html"))


def preencher(qual: Template | str, /, **campos) -> Html:
    """Preenche um modelo (ou o nome de um fragmento) escapando todo valor que não seja `Html`."""
    t = FRAGMENTOS[qual] if isinstance(qual, str) else qual
    return Html(t.substitute({k: _valor(v) for k, v in campos.items()}))


def juntar(trechos) -> Html:
    return Html("\n".join(trechos))


def estilo_css() -> str:
    return _ler("estilo.css")


def pagina(titulo: str, conteudo: Html, recarregar_s: int | None) -> str:
    """Documento completo. `recarregar_s` None desliga o `<meta refresh>` (modo --demo)."""
    meta = Html(f'<meta http-equiv="refresh" content="{int(recarregar_s)}">\n') if recarregar_s else Html("")
    return preencher(BASE, titulo=titulo, conteudo=conteudo, recarregar=meta)


# ---------- números em português ----------

def _milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _decimal(v: float, casas: int) -> str:
    return f"{v:,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def usd(v, sem_preco: bool = False) -> str:
    """US$ 4,87; valores entre 0 e 1 centavo viram "< US$ 0,01"; None = "—". `sem_preco` acrescenta o "*"."""
    estrela = "*" if sem_preco else ""
    if v is None:
        return "—" + estrela
    if 0 < v < 0.005:
        return "< US$ 0,01" + estrela
    return f"US$ {_decimal(v, 2)}{estrela}"


def tokens(n) -> str:
    """3,20 M · 312 k · 41 k · 950."""
    n = n or 0
    if n >= 1_000_000_000:
        return f"{_decimal(n / 1e9, 2)} B"
    if n >= 1_000_000:
        return f"{_decimal(n / 1e6, 2)} M"
    if n >= 1_000:
        return f"{round(n / 1000)} k"
    return str(int(n))


def inteiro(n) -> str:
    return _milhar(int(n or 0))


def pct(v, casas: int = 0) -> str:
    return "—" if v is None else f"{_decimal(v * 100, casas)}%"


def duracao(s) -> str:
    """58 min · 2h 11m · 40 s."""
    s = int(round(s or 0))
    if s < 60:
        return f"{s} s"
    h, resto = divmod(s, 3600)
    m = resto // 60
    return f"{h}h {m:02d}m" if h else f"{m} min"


def hora_local(ts: str | None, segundos: bool = False) -> str:
    t = diagnostico._t(ts) if ts else None
    if t is None:
        return "—"
    return t.astimezone().strftime("%H:%M:%S" if segundos else "%H:%M")


def data_hora_local(ts: str | None) -> str:
    t = diagnostico._t(ts) if ts else None
    return "—" if t is None else t.astimezone().strftime("%d/%m %H:%M")


def data_por_extenso(d: date) -> str:
    return f"{DIAS_DA_SEMANA[d.weekday()]}, {d:%d/%m/%Y}"


def nome_do_modelo(modelo: str | None, curto: bool = False) -> str:
    """claude-opus-5-5 -> "Claude Opus 5.5" (ou "Opus 5.5"); nomes fora do padrão ficam como estão."""
    if not modelo:
        return "?"
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", modelo)
    if not m:
        return modelo
    familia, maior, menor = m.group(1).capitalize(), m.group(2), m.group(3)
    nome = f"{familia} {maior}{'.' + menor if menor else ''}"
    return nome if curto else f"Claude {nome}"


def _largura(v, maximo) -> str:
    """Porcentagem para `style="width"` (texto com ponto, uma casa); 0 se não há máximo."""
    if not maximo or not v:
        return "0"
    return f"{max(0.0, min(100.0, v / maximo * 100)):.1f}"


# ---------- períodos e URLs ----------

NOMES_DO_PERIODO = dict(PERIODOS)
QUANDO = {"hoje": "hoje", "7d": "nos últimos 7 dias", "30d": "nos últimos 30 dias", "mes": "neste mês",
          "tudo": "em todo o histórico"}


def url_da_visao(periodo: str = "hoje", projeto: str | None = None) -> str:
    """URL canônica de uma visão: o que é padrão (hoje, todos os projetos) não aparece na URL."""
    params = []
    if periodo != "hoje":
        params.append(("periodo", periodo))
    if projeto:
        params.append(("projeto", projeto))
    return "/" + ("?" + urlencode(params) if params else "")


def _texto_do_periodo(d: dict) -> str:
    """Data (hoje) ou intervalo do período, no cabeçalho dos filtros."""
    hoje = date.fromisoformat(d["hoje"])
    if d["periodo"] == "hoje":
        return data_por_extenso(hoje)
    if d["periodo"] == "tudo" or not d["desde"]:
        return f"todo o histórico, até {hoje:%d/%m/%Y}"
    return f"{date.fromisoformat(d['desde']):%d/%m} a {hoje:%d/%m/%Y}"


# ---------- blocos da tela ----------

def _abas(d: dict) -> Html:
    return juntar(preencher("aba_atual" if codigo == d["periodo"] else "aba",
                            href=url_da_visao(codigo, d["projeto"]), nome=nome)
                  for codigo, nome in PERIODOS)


def _status(d: dict, demo: bool, recarregar_s: int) -> Html:
    quando = hora_local(d["momento"], segundos=True)
    if demo:
        return Html(f"<strong>Demonstração</strong> · dados fictícios · horário fixo {_valor(quando)}")
    return Html(f"<strong>Ao vivo</strong> · atualizado às {_valor(quando)} · recarrega a cada {int(recarregar_s)} s")


def _chips(d: dict) -> Html:
    nomes = {"claude_code": "Claude Code", "copilot": "Copilot"}
    ferramentas = d["ferramentas"]
    ferramenta = nomes.get(ferramentas[0], ferramentas[0]) if len(ferramentas) == 1 else "todas"
    projetos = list(d["projetos_disponiveis"])
    if d["projeto"] and d["projeto"] not in projetos:
        projetos.append(d["projeto"])                    # filtro vindo da URL, mesmo sem dados, continua visível e removível
    chips = [preencher("chip", rotulo="Ferramenta", valor=ferramenta)]
    if projetos:
        opcoes = [preencher("pill_atual" if not d["projeto"] else "pill", href=url_da_visao(d["periodo"]), nome="todos")]
        opcoes += [preencher("pill_atual" if p == d["projeto"] else "pill", href=url_da_visao(d["periodo"], p), nome=p)
                   for p in projetos]
        chips.append(preencher("grupo", rotulo="Projeto", opcoes=juntar(opcoes)))
    else:
        chips.append(preencher("chip", rotulo="Projeto", valor="todos"))
    chips.append(preencher("chip", rotulo="Modelo", valor="todos"))
    return juntar(chips)


def _avisos(d: dict, extras: list[str]) -> Html:
    quando = QUANDO[d["periodo"]]
    trechos = [preencher("aviso", texto=e) for e in extras]
    if d["projeto"] and not d["chamadas"] and not d["sessoes"]:
        trechos.append(preencher("aviso", texto=f"Nenhum dado do projeto “{d['projeto']}” {quando}."))
    if d["sem_sequencia"] or d["sem_categoria"]:
        partes = []
        if d["sem_sequencia"]:
            partes.append(f"{inteiro(d['sem_sequencia'])} chamada(s) sem turno e sequência")
        if d["sem_categoria"]:
            partes.append(f"{inteiro(d['sem_categoria'])} erro(s) de tool sem categoria")
        texto = Html(_valor(f"Parte dos dados {'de hoje' if d['periodo'] == 'hoje' else 'do período'} foi gravada por uma "
                            f"versão antiga do tc ({' e '.join(partes)}). ")
                     + "Feche os <code>tc live</code>/<code>tc painel</code> abertos antes da atualização e rode "
                       "<code>tc ingest --tudo</code>.")
        trechos.append(preencher("aviso", texto=texto))
    if d["sem_preco"]:
        trechos.append(preencher("aviso", texto=f"{inteiro(d['sem_preco'])} chamada(s) {'de hoje' if d['periodo'] == 'hoje' else 'do período'} "
                                                "sem preço (modelo fora da tabela ou sem custo no modo atual): os custos com * "
                                                "estão incompletos."))
    return juntar(trechos)


def _sparkline(valores: list[float], descricao: str) -> Html:
    """Custo acumulado dos `valores` (pontos num quadro de 200 x 40)."""
    total = sum(valores)
    if not total:
        return preencher("sparkline", rotulo="Sem custo", pontos="0,40 200,40")
    acumulado, pontos = 0.0, ["0,40"]
    for k, v in enumerate(valores, 1):
        acumulado += v
        pontos.append(f"{k * 200 / len(valores):.1f},{40 - acumulado / total * 36:.1f}")
    return preencher("sparkline", pontos=" ".join(pontos), rotulo=f"Custo acumulado {descricao}, até {usd(acumulado)}")


def _faixa_tokens(t: dict) -> Html:
    total = sum(t.values())
    ordem = (("input", "cor-input", "input"), ("output", "cor-output", "output"),
             ("cache_write", "cor-gravacao", "gravação de cache"), ("cache_read", "cor-leitura", "leitura de cache"))
    partes = juntar(preencher("parte", classe=classe, largura=_largura(t[chave], total),
                              dica=f"{nome}: {tokens(t[chave])} ({pct(t[chave] / total if total else None)})")
                    for chave, classe, nome in ordem)
    rotulo = "Divisão dos tokens: " + ", ".join(f"{pct(t[c] / total if total else None)} {n}" for c, _, n in ordem)
    return preencher("faixa", partes=partes, rotulo=rotulo)


def _trilho(fracao, classe: str, rotulo: str, meta: float | None = None) -> Html:
    marca = preencher("meta", posicao=_largura(meta, 1), dica=f"meta {pct(meta)}") if meta is not None else Html("")
    return preencher("trilho", largura=_largura(fracao or 0, 1), classe=classe, rotulo=rotulo, marca=marca)


def _grafico_horas(horas: list[float], hora_atual: int) -> tuple[Html, str]:
    """Gasto por hora do dia (a tela Hoje): janela de `COLUNAS_DE_HORA` horas; devolve (HTML, nota do cabeçalho)."""
    com_custo = [h for h, v in enumerate(horas) if v > 0]
    ini = min(com_custo[0] if com_custo else hora_atual, hora_atual)
    ini = max(0, min(ini, 24 - COLUNAS_DE_HORA))
    janela = range(ini, ini + COLUNAS_DE_HORA)
    maior = max((horas[h] for h in janela), default=0)
    colunas, eixo, descricao = [], [], []
    for h in janela:
        v = horas[h]
        if h > hora_atual:
            classe, valor, altura, dica = "hora-futura", "", ALTURA_MIN_PX, f"{h:02d}h: ainda não chegou"
        else:
            classe = "hora-atual" if h == hora_atual else ""
            valor = _decimal(v, 2) if v > 0 else "—"
            altura = max(ALTURA_MIN_PX, round(v / maior * ALTURA_MAX_PX)) if maior and v > 0 else ALTURA_MIN_PX
            dica = f"{h:02d}h: {usd(v)}" + (" (até agora)" if h == hora_atual else "")
            if v > 0:
                descricao.append(f"{h}h {_decimal(v, 2)}")
        colunas.append(preencher("hora", classe=classe, valor=valor, altura=altura, dica=dica))
        eixo.append(preencher("hora_rotulo", rotulo=f"{h:02d}h"))
    rotulo = ("Gasto por hora, em dólares: " + "; ".join(descricao)) if descricao else "Sem gasto hoje"
    return (preencher("horas", rotulo=rotulo, colunas=juntar(colunas), eixo=juntar(eixo), densidade=""),
            "hora atual em destaque")


def _grafico_dias(serie: dict, hoje_iso: str) -> tuple[Html, str]:
    """Gasto por dia dos períodos mais longos; devolve (HTML, nota do cabeçalho)."""
    itens = serie["dias"]
    n = len(itens)
    maior = max((i["custo_usd"] for i in itens), default=0)
    denso = n > COLUNAS_SEM_VALOR
    passo = 1 if n <= 12 else -(-n // 10)                      # cerca de 10 rótulos no eixo, a partir do dia mais recente
    colunas, eixo, descricao = [], [], []
    for k, item in enumerate(itens):
        v = item["custo_usd"]
        dia_ = date.fromisoformat(item["dia"])
        classe = "hora-atual" if item["dia"] == hoje_iso else ""
        valor = "" if denso else (_decimal(v, 2) if v > 0 else "—")
        altura = max(ALTURA_MIN_PX, round(v / maior * ALTURA_MAX_PX)) if maior and v > 0 else ALTURA_MIN_PX
        dica = f"{dia_:%d/%m} ({DIAS_ABREVIADOS[dia_.weekday()]}): {usd(v)}" + (" (hoje, até agora)" if classe else "")
        if v > 0:
            descricao.append(f"{dia_:%d/%m} {_decimal(v, 2)}")
        colunas.append(preencher("hora", classe=classe, valor=valor, altura=altura, dica=dica))
        eixo.append(preencher("hora_rotulo", rotulo=f"{dia_:%d/%m}" if (n - 1 - k) % passo == 0 else ""))
    rotulo = ("Gasto por dia, em dólares: " + "; ".join(descricao)) if descricao else "Sem gasto no período"
    nota = "sem gasto no período"
    if maior:
        pico = max(itens, key=lambda i: i["custo_usd"])
        nota = f"pico {usd(maior)} em {date.fromisoformat(pico['dia']):%d/%m} · hoje em destaque"
    if serie["truncado"]:
        nota += f" · últimos {n} dias"
    return (preencher("horas", rotulo=rotulo, colunas=juntar(colunas), eixo=juntar(eixo), densidade="densa" if denso else ""),
            nota)


def _bloco_rosca(d: dict) -> Html:
    cpt = d["custo_por_tipo"]
    total_dividido = cpt["calculado_usd"]
    if not total_dividido and not cpt["registrado_usd"]:
        return preencher("sem_dados", texto=f"Sem custo {QUANDO[d['periodo']]}.")
    fatias, itens, rotulo, inicio = [], [], [], 0.0
    base = total_dividido + cpt["registrado_usd"]
    for chave in ("output", "cache_write", "cache_read", "input"):
        v = cpt["tipos"][chave]
        fim = inicio + v / base * 100
        fatias.append(f"{CORES_DOS_TIPOS[chave]} {inicio:.2f}% {fim:.2f}%")
        itens.append(preencher("legenda", classe=CLASSES_DOS_TIPOS[chave], nome=NOMES_DOS_TIPOS[chave],
                               valor=f"{pct(v / base)} · {usd(v)}"))
        rotulo.append(f"{NOMES_DOS_TIPOS[chave].lower()} {pct(v / base)}")
        inicio = fim
    if cpt["registrado_usd"]:
        fatias.append(f"#4A5466 {inicio:.2f}% 100%")
        itens.append(preencher("legenda", classe="cor-registrado", nome="Registrado no log (sem divisão)",
                               valor=f"{pct(cpt['registrado_usd'] / base)} · {usd(cpt['registrado_usd'])}"))
    nota = Html("")
    if total_dividido:
        nota = preencher("nota", texto=f"Sem cache, as mesmas chamadas custariam ~{usd(cpt['sem_cache_usd'])} "
                                       "(contexto inteiro cobrado como input, com os tokens reais).")
    sub = "hoje" if d["periodo"] == "hoje" else NOMES_DO_PERIODO[d["periodo"]].lower()
    return preencher("rosca", fatias=", ".join(fatias), rotulo="Rosca: " + ", ".join(rotulo),
                     total=usd(d["custo_usd"], bool(d["sem_preco"])), sub=sub, itens=juntar(itens), nota=nota)


def _bloco_projetos(d: dict) -> Html:
    projetos = d["projetos"]
    if not projetos:
        return preencher("sem_dados", texto=f"Nenhum projeto com custo {QUANDO[d['periodo']]}.")
    maior = projetos[0]["custo_usd"]
    return juntar(preencher("item", nome=p["projeto"], classe="cheio-projeto", largura=_largura(p["custo_usd"], maior),
                            valor=f"{usd(p['custo_usd'])} · {p['sessoes']} {'sessão' if p['sessoes'] == 1 else 'sessões'}",
                            dica=p["projeto"])
                  for p in projetos[:6])


def _bloco_modelos(d: dict) -> Html:
    modelos = d["modelos"]
    if not modelos:
        return preencher("sem_dados", texto=f"Nenhuma chamada {QUANDO[d['periodo']]}.")
    total = sum(m["custo_usd"] or 0 for m in modelos)
    maior = max((m["custo_usd"] or 0) for m in modelos)
    return juntar(preencher("item", nome=nome_do_modelo(m["modelo"]), classe="cheio-modelo",
                            largura=_largura(m["custo_usd"] or 0, maior), dica=m["modelo"],
                            valor=(f"{usd(m['custo_usd'], bool(m['sem_preco']))} · "
                                   f"{pct((m['custo_usd'] or 0) / total) if total else '—'}"))
                  for m in modelos[:6])


def _bloco_tools(d: dict) -> Html:
    lista = d["lista_tools"]
    if not lista:
        return preencher("sem_dados", texto=f"Nenhuma tool {QUANDO[d['periodo']]}.")
    maior = lista[0]["chamadas"]
    trechos = []
    for t in lista[:6]:
        taxa = t["taxa_erro"] or 0
        valor = inteiro(t["chamadas"]) + (f" · {pct(taxa, 1)}" if t["erros"] else "")
        dica = f"{t['tool']}: {t['chamadas']} chamada(s), {t['erros']} com erro"
        if t["recusadas"]:
            dica += f", {t['recusadas']} recusada(s) por você (não contam como erro)"
        trechos.append(preencher("tool", nome=t["tool"], largura=_largura(t["chamadas"], maior),
                                 erro=f"{taxa * 100:.1f}", valor=valor, dica=dica,
                                 classe="tool-valor-erro" if taxa >= TAXA_ERRO_DESTAQUE else ""))
    return juntar(trechos)


def _selos(sinais: list[dict]) -> Html:
    """Um selo com os sinais de severidade média e alta, na cor do mais grave (os de baixa ficam no detalhe da sessão)."""
    textos, dicas, pior = [], [], "media"
    for s in sinais:
        if s["severidade"] not in ("alta", "media"):
            continue
        texto = diagnostico.ROTULOS[s["sinal"]]
        if s["sinal"] == "pausa_longa":
            texto = f"{s['dados']['pausas']} pausas longas"
        textos.append(texto)
        dicas.append(f"{texto}: severidade {'média' if s['severidade'] == 'media' else 'alta'}")
        if s["severidade"] == "alta":
            pior = "alta"
    if not textos:
        return preencher("sem_sinal")
    return preencher("selo", severidade=pior, texto=" · ".join(textos), dica="; ".join(dicas))


def _bloco_sessoes(d: dict, meta_cache: float) -> Html:
    if not d["sessoes"]:
        extra = " Assim que você usar o Claude Code, ela aparece aqui." if d["periodo"] == "hoje" else ""
        return preencher("sem_dados", texto=f"Nenhuma sessão {QUANDO[d['periodo']]}.{extra}")
    linhas = []
    for s in d["sessoes"]:
        cache = s["cache_taxa"]
        inicio = hora_local(s["inicio"]) if d["periodo"] == "hoje" else data_hora_local(s["inicio"])
        linhas.append(preencher(
            "linha", inicio=inicio, href=f"/sessao/{quote(s['sessao'][:8], safe='')}",
            projeto=s["projeto"], andamento=preencher("andamento") if s["em_andamento"] else Html(""),
            modelo=nome_do_modelo(s["modelo"], curto=True), duracao=duracao(s["duracao_s"]), ativo=duracao(s["ativo_s"]),
            chamadas=inteiro(s["chamadas"]), cache=pct(cache),
            classe_cache="cache-baixo" if cache is not None and cache < meta_cache else "",
            erros=inteiro(s["erros_tool"]), custo=usd(s["custo_usd"]), sinais=_selos(s["sinais"])))
    return preencher("tabela", linhas=juntar(linhas))


def visao(d: dict, limites: dict, demo: bool, recarregar_s: int, endereco: str, avisos: list[str] | None = None) -> str:
    """A tela inteira (Hoje, 7 dias, 30 dias, mês ou tudo), a partir de `consultas.visao`.

    `avisos` são textos simples (por exemplo, "período inválido") mostrados no topo; entram escapados.
    """
    periodo = d["periodo"]
    nome = NOMES_DO_PERIODO[periodo]
    momento = datetime.fromisoformat(d["momento"])
    t = d["tokens"]
    total_tokens = sum(t.values())
    tools_ = d["tools"]
    sessoes = d["sessoes"]
    serie = d["serie"]
    em_andamento = sum(1 for s in sessoes if s["em_andamento"])
    meta = limites["cache_taxa_min"]
    abaixo = sum(1 for s in sessoes if any(x["sinal"] == "cache_baixo" for x in s["sinais"]))
    taxa_tools = tools_["erros"] / tools_["chamadas"] if tools_["chamadas"] else None
    ativo, total = d["ativo_s"], d["duracao_s"]

    if serie["tipo"] == "hora":
        horas, hora_atual = serie["valores"], momento.astimezone().hour
        grafico, grafico_nota = _grafico_horas(horas, hora_atual)
        grafico_titulo = "Gasto por hora"
        com_custo = [h for h, v in enumerate(horas) if v > 0]
        ini, fim = min(com_custo[0] if com_custo else hora_atual, hora_atual), max(hora_atual, com_custo[-1] if com_custo else 0)
        spark = _sparkline(horas[ini:fim + 1], f"das {ini:02d}h às {fim:02d}h")
        custo_nota = f"+{usd(horas[hora_atual])} nesta hora · equivalente de API"
        custo_rotulo = "Custo hoje"
    else:
        grafico, grafico_nota = _grafico_dias(serie, d["hoje"])
        grafico_titulo = "Gasto por dia"
        valores = [i["custo_usd"] for i in serie["dias"]]
        spark = _sparkline(valores, f"de {date.fromisoformat(serie['dias'][0]['dia']):%d/%m} a "
                                    f"{date.fromisoformat(serie['dias'][-1]['dia']):%d/%m}")
        custo_nota = f"+{usd(valores[-1])} hoje · equivalente de API"
        custo_rotulo = f"Custo · {nome.lower()}"

    nota_tools = f"Erros de API: {inteiro(d['erros_api'])}" if d["erros_api"] else "Nenhum erro de API"
    if tools_["recusadas"]:
        nota_tools += f" · {inteiro(tools_['recusadas'])} recusada(s) por você"
    fonte = ("Fonte: banco de demonstração (dados fictícios)" if demo else "Fonte: ~/.claude/projects")
    fonte += (f" · último ingest {hora_local(d['ultimo_ingest'], segundos=True)}"
              f" · {inteiro(d['linhas_ilegiveis'])} linhas ilegíveis · {endereco}")
    dica = "clique em uma sessão para ver o detalhe" if sessoes else ""
    if d["sessoes_cortadas"]:
        dica = f"mostrando as {len(sessoes)} mais recentes de {inteiro(d['sessoes_total'])} · " + dica

    conteudo = preencher(
        HOJE,
        abas=_abas(d), classe_ponto="ponto" if demo else "ponto ponto-vivo", status=_status(d, demo, recarregar_s),
        chips=_chips(d), modo=d["modo"], data_extenso=_texto_do_periodo(d), avisos=_avisos(d, avisos or []),
        custo_rotulo=custo_rotulo, custo=usd(d["custo_usd"] or 0, bool(d["sem_preco"])), sparkline=spark,
        custo_nota=custo_nota,
        tokens=tokens(total_tokens), faixa_tokens=_faixa_tokens(t),
        tokens_nota=Html(_valor(f"input {tokens(t['input'])} · output {tokens(t['output'])}") + "<br>"
                         + _valor(f"cache: {tokens(t['cache_read'])} lidos · {tokens(t['cache_write'])} gravados")),
        cache=pct(d["cache_taxa"]),
        trilho_cache=_trilho(d["cache_taxa"], "cor-gravacao", f"Taxa de cache de {pct(d['cache_taxa'])}, meta de {pct(meta)}",
                             meta),
        cache_nota=f"meta ≥ {pct(meta)} · {abaixo} {'sessão' if abaixo == 1 else 'sessões'} abaixo",
        chamadas=inteiro(d["chamadas"]),
        chamadas_nota=(f"{inteiro(d['prompts'])} prompts · "
                       + (f"{round(d['chamadas'] / d['prompts'])} chamadas por prompt" if d["prompts"] else "sem prompts")),
        sessoes_nota=(f"{d['sessoes_total']} {'sessão' if d['sessoes_total'] == 1 else 'sessões'} · "
                      f"{em_andamento} em andamento"),
        tools=inteiro(tools_["chamadas"]),
        tools_erro=f"{inteiro(tools_['erros'])} com erro ({pct(taxa_tools, 1)})" if tools_["erros"] else "nenhuma com erro",
        classe_tools_erro="kpi-nota kpi-erro" if tools_["erros"] else "kpi-nota",
        tools_nota=nota_tools,
        ativo=duracao(ativo),
        trilho_ativo=_trilho(ativo / total if total else 0, "cor-ativo",
                             f"Tempo ativo é {pct(ativo / total if total else None)} do tempo total de sessão"),
        ativo_nota=f"de {duracao(total)} de sessão (pausas > 5 min descontadas)",
        grafico_titulo=grafico_titulo, grafico_nota=grafico_nota, grafico=grafico, rosca=_bloco_rosca(d),
        projetos=_bloco_projetos(d), modelos=_bloco_modelos(d), lista_tools=_bloco_tools(d),
        sessoes_titulo="Sessões de hoje" if periodo == "hoje" else f"Sessões · {nome.lower()}", sessoes_dica=dica,
        tabela=_bloco_sessoes(d, meta), fonte=fonte,
    )
    return pagina(f"tc web · {nome}", conteudo, None if demo else recarregar_s)


def recado(titulo: str, texto: str) -> str:
    """Página simples (não encontrada, em construção), sem recarregar."""
    return pagina(f"tc web · {titulo}", preencher("recado", titulo=titulo, texto=texto), None)
