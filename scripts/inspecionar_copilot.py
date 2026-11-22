"""Mostra a ESTRUTURA dos logs do Copilot no VS Code, sem nenhum valor.

Uso (no notebook onde o Copilot roda):
    python scripts/inspecionar_copilot.py > estrutura_copilot.txt

A saída tem só nomes de campos e tipos (str, int, ...), nunca o conteúdo das conversas.
Mesmo assim, revise o arquivo antes de compartilhar.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys


def raiz_vscode() -> list[str]:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    candidatos = [os.path.join(appdata, n, "User") for n in ("Code", "Code - Insiders", "VSCodium")]
    candidatos += [os.path.expanduser("~/Library/Application Support/Code/User"),  # macOS
                   os.path.expanduser("~/.config/Code/User")]                      # Linux
    return [c for c in candidatos if os.path.isdir(c)]


def esqueleto(v, prof=0):
    if prof > 8:
        return "..."
    if isinstance(v, dict):
        return {k: esqueleto(x, prof + 1) for k, x in v.items()}
    if isinstance(v, list):
        # junta as chaves de até 5 itens, para mostrar campos que variam entre itens
        if v and all(isinstance(i, dict) for i in v[:5]):
            junto: dict = {}
            for i in v[:5]:
                for k, x in i.items():
                    junto.setdefault(k, esqueleto(x, prof + 2))
            return [junto]
        return [esqueleto(v[0], prof + 1)] if v else []
    return type(v).__name__


def main() -> None:
    raizes = raiz_vscode()
    if not raizes:
        print("Pasta de dados do VS Code não encontrada.")
        sys.exit(1)
    for base in raizes:
        print(f"== {base}")
        sessoes = glob.glob(os.path.join(base, "workspaceStorage", "*", "chatSessions", "*.json*"))
        sessoes += glob.glob(os.path.join(base, "globalStorage", "emptyWindowChatSessions", "*.json*"))
        transcr = glob.glob(os.path.join(base, "workspaceStorage", "*", "GitHub.copilot-chat", "transcripts", "*"))
        traces = glob.glob(os.path.join(base, "**", "agent-traces.db"), recursive=True)
        print(f"chatSessions: {len(sessoes)} arquivo(s) | transcripts: {len(transcr)} | agent-traces.db: {len(traces)}")

        if sessoes:
            arq = max(sessoes, key=os.path.getmtime)
            print(f"\n-- estrutura de um chatSession ({os.path.splitext(arq)[1]}):")
            with open(arq, encoding="utf-8", errors="replace") as f:
                texto = f.read()
            linhas = texto.splitlines() if arq.endswith(".jsonl") else [texto]
            print(f"   {len(linhas)} linha(s)")
            vistos = set()
            for linha in linhas:
                try:
                    obj = json.loads(linha)
                except json.JSONDecodeError:
                    continue
                forma = json.dumps(esqueleto(obj), indent=1, ensure_ascii=False)
                if forma in vistos:
                    continue
                vistos.add(forma)
                print(forma[:6000])
                if len(vistos) >= 4:
                    break

        for db in traces[:1]:
            print("\n-- tabelas do agent-traces.db (só nomes e tipos de colunas):")
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for (nome,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                cols = [(c[1], c[2]) for c in con.execute(f"PRAGMA table_info('{nome}')")]
                n = con.execute(f"SELECT COUNT(*) FROM '{nome}'").fetchone()[0]
                print(f"   {nome} ({n} linhas): {cols}")
            con.close()


if __name__ == "__main__":
    main()
