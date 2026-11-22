"""Gera o banco de demonstração do `tc web --demo` (dados fictícios e determinísticos) num arquivo.

Uso: python scripts/gerar_demo.py [caminho]   (padrão: demo.db na pasta atual; *.db fica fora do git)

O `tc web --demo` gera o mesmo banco sozinho numa pasta temporária; este script serve para abrir o banco à mão
(por exemplo, `tc --db demo.db resumo`). A lógica está em `token_controler/demo.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from token_controler import demo  # noqa: E402

if __name__ == "__main__":
    destino = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
    print(f"banco de demonstração: {demo.gerar(destino)} (agora congelado em {demo.AGORA_LOCAL:%d/%m/%Y %H:%M:%S}, local)")
