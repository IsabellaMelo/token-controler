"""Classificação de comandos de shell (T14/T15). Comandos escritos à mão, todos sintéticos."""

import pytest

from token_controler.comandos import classificar_comando as cc


@pytest.mark.parametrize("comando", [
    "pytest", "python -m pytest -q tests/", ".venv\\Scripts\\python -m pytest", "npm test", "npm run test -- --watch",
    "npx vitest run", "cargo test", "go test ./...", "dotnet test", "cd app && pytest -x",
])
def test_testes(comando):
    assert cc(comando) == "teste"


@pytest.mark.parametrize("comando", [
    "npm run build", "docker compose up", "pip install -e .", "python -m pip install x", "make all",
    "npm install", "kubectl apply -f x.yaml", "terraform plan",
])
def test_build(comando):
    assert cc(comando) == "build"


@pytest.mark.parametrize("comando", [
    "git commit -m 'msg'", "git push origin main", "git add -A && git commit -m x", "git checkout -b nova",
    "git -C pasta commit -m x", "git stash", "git branch -d x",
])
def test_git_que_altera(comando):
    assert cc(comando) == "git"


@pytest.mark.parametrize("comando", [
    "ls -la", "cat arquivo.txt", "rg padrao src/", "grep -rn foo .", "git log --oneline", "git status",
    "git diff HEAD~1", "git -C pasta status", "cat a.txt | head -n 5", "ls && pwd", "find . -name '*.py'",
    "Get-Content x.log | Select-String erro", "Get-ChildItem -Recurse | Select-Object Name", "echo oi",
])
def test_leitura(comando):
    assert cc(comando) == "leitura"


@pytest.mark.parametrize("comando", [
    "python script.py", "rm -rf build", "sed -i s/a/b/ x.txt", "cat a > b", "echo x >> log.txt",
    "find . -name x -delete", "cat x && rm y", "mkdir novo", "node app.js", "git", "Remove-Item x",
    "", "   ", None, 123, ["ls"],
])
def test_outro(comando):
    assert cc(comando) == "outro"


def test_palavras_dentro_de_aspas_nao_classificam():
    # "test" e "build" aparecem só no texto da mensagem do commit, não são comandos
    assert cc("git commit -m 'adiciona test e build'") == "git"
    assert cc("echo \"rode pytest depois\"") == "leitura"


def test_prefixos_e_variaveis_de_ambiente_sao_ignorados():
    assert cc("sudo ls /etc") == "leitura"
    assert cc("FOO=1 cat x") == "leitura"
    assert cc("FOO=1 python x.py") == "outro"


def test_redirecionamento_torna_o_comando_nao_leitura_mas_seta_nao():
    assert cc("ls > saida.txt") == "outro"
    assert cc("echo a>b") == "outro"
    assert cc("echo 'a>b'") == "leitura"  # dentro de aspas
