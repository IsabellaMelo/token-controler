"""Formatação de números para o terminal (tokens, dólares, porcentagem, duração)."""

from __future__ import annotations


def fmt_tokens(n) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def fmt_usd(v) -> str:
    return "-" if v is None else f"${v:,.2f}"


def fmt_pct(v) -> str:
    return "-" if v is None else f"{v:.0%}"


def fmt_dur(s) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    h, r = divmod(s, 3600)
    m, _ = divmod(r, 60)
    return f"{h}h{m:02d}" if h else f"{m}min"
