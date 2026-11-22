"""Classifica um comando de shell em uma classe grossa, sem guardar o texto do comando.

O comando é lido na ingestão só para escolher a classe; o banco recebe apenas o nome da classe
(regra 1 do AGENTS.md). Ideia inspirada no CodeBurn (comandos "só de leitura" não verificam uma
edição), reimplementada aqui. Comando desconhecido cai em `outro`, o padrão conservador.
"""

from __future__ import annotations

import re

TESTE, BUILD, GIT, LEITURA, OUTRO = "teste", "build", "git", "leitura", "outro"

# Nomes das tools que rodam comandos de shell.
TOOLS_SHELL = frozenset({"Bash", "BashTool", "PowerShell", "PowerShellTool"})

_PREFIXOS = frozenset({"sudo", "doas", "npx", "bunx", "time", "nice", "nohup", "stdbuf", "command", "call"})

_SO_LEITURA = frozenset({
    "rg", "grep", "egrep", "fgrep", "ag", "cat", "head", "tail", "less", "more", "ls", "dir", "find", "fd",
    "tree", "wc", "stat", "file", "du", "df", "which", "where", "type", "pwd", "printenv", "env", "echo",
    "readlink", "realpath", "basename", "dirname", "jq", "diff", "findstr", "sort", "uniq", "cut", "nl",
    # PowerShell
    "get-content", "get-childitem", "get-item", "get-location", "get-command", "get-process", "get-date",
    "select-string", "select-object", "where-object", "sort-object", "measure-object", "test-path",
    "format-table", "format-list", "out-string", "resolve-path", "split-path", "join-path", "gc", "gci",
})

# Subcomandos do git que só inspecionam. Fora daqui (commit, push, branch...), o comando é `git`.
_GIT_LEITURA = frozenset({"log", "diff", "status", "show", "blame", "grep", "shortlog", "describe",
                          "rev-parse", "ls-files"})

_TESTES = re.compile(
    r"\b(pytest|py\.test|unittest|vitest|jest|mocha|tox|rspec|phpunit)\b"
    r"|\b(npm|yarn|pnpm)\s+(run\s+)?test\b"
    r"|\b(cargo|go|dotnet|mvn|gradle|gradlew|swift|deno|bun)\s+test\b",
    re.IGNORECASE,
)
_BUILDS = re.compile(
    r"\b(docker|docker-compose|kubectl|terraform|pm2|systemctl|msbuild|make)\b"
    r"|\b(npm|yarn|pnpm)\s+(run\s+)?(build|publish|ci|install|start|dev)\b"
    r"|\b(pip|pip3|brew|apt|apt-get|choco|winget)\s+install\b|\bpython\s+-m\s+pip\s+install\b"
    r"|\b(cargo|dotnet|mvn|gradle|gradlew)\s+(build|package|publish|install)\b|\bdeploy\b",
    re.IGNORECASE,
)
_ASPAS = re.compile(r'"[^"]*"|\'[^\']*\'')
_SEPARADORES = re.compile(r"&&|\|\||;|\||\n")
_REDIRECIONA = re.compile(r"(?<![<>=-])>{1,2}(?!=)")  # `>` e `>>`, mas não `->` nem `>=`
_PERIGOSOS_NA_LEITURA = ("-delete", "-exec", "-ok")


def _sem_aspas(comando: str) -> str:
    """Apaga o conteúdo entre aspas, para que palavras de mensagens e padrões não classifiquem o comando."""
    return _ASPAS.sub(lambda m: " " * len(m.group(0)), comando)


def _base(segmento: str) -> tuple[str, list[str]]:
    """Nome do programa do trecho (sem pasta, sem extensão, em minúsculas) e os demais tokens."""
    tokens = segmento.split()
    i = 0
    while i < len(tokens) and (re.match(r"^\w+=", tokens[i]) or tokens[i].lower() in _PREFIXOS):
        i += 1
    if i >= len(tokens):
        return "", []
    nome = tokens[i].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if nome.endswith(ext):
            nome = nome[: -len(ext)]
    return nome, tokens[i + 1:]


def _sub_git(resto: list[str]) -> str | None:
    """Subcomando do git, pulando opções globais (`git -C pasta -c k=v status` -> `status`)."""
    i = 0
    while i < len(resto):
        if resto[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif resto[i].startswith("-"):
            i += 1
        else:
            return resto[i]
    return None


def _so_leitura(segmentos: list[str]) -> bool:
    """True quando TODOS os trechos só inspecionam. Um trecho que altera (ou desconhecido) torna tudo não-leitura."""
    visto = False
    for segmento in segmentos:
        nome, resto = _base(segmento)
        if not nome:
            continue
        visto = True
        if nome == "git":
            if _sub_git(resto) not in _GIT_LEITURA:
                return False
        elif nome not in _SO_LEITURA or any(t in _PERIGOSOS_NA_LEITURA for t in resto):
            return False
    return visto


def _tem_git_que_altera(segmentos: list[str]) -> bool:
    for segmento in segmentos:
        nome, resto = _base(segmento)
        sub = _sub_git(resto) if nome == "git" else None
        if sub is not None and sub not in _GIT_LEITURA:
            return True
    return False


def classificar_comando(comando) -> str:
    """Classe do comando: teste, build, git, leitura ou outro. Qualquer valor que não seja texto é `outro`."""
    if not isinstance(comando, str) or not comando.strip():
        return OUTRO
    limpo = _sem_aspas(comando)
    if _TESTES.search(limpo):
        return TESTE
    if _BUILDS.search(limpo):
        return BUILD
    segmentos = [s.strip() for s in _SEPARADORES.split(limpo) if s.strip()]
    if _tem_git_que_altera(segmentos):
        return GIT
    if _REDIRECIONA.search(limpo):
        return OUTRO
    return LEITURA if _so_leitura(segmentos) else OUTRO
