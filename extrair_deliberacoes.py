#!/usr/bin/env python3
"""
extrair_deliberacoes.py — separa cada item dos acórdãos baixados (acordaos/)
e classifica pelo verbo: determinação, recomendação, ciência, alerta ou
outra decisão. Grava site/deliberacoes.json, que a aba "Etapas e prazos" lê.

Tudo sai do texto público do acórdão. A classificação é por regra de texto
(o verbo que abre o item); o texto integral do item vai junto, para conferência.
"""
from __future__ import annotations

import json
import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
PASTA = os.path.join(AQUI, "acordaos")
INDICE = os.path.join(PASTA, "indice.json")
SAIDA = os.path.join(AQUI, "site", "deliberacoes.json")

TIPOS = [  # ordem importa: a primeira que casar vence
    ("alerta",        r"alertar|expedir alerta"),
    ("determinacao",  r"determinar|fixar(?:, com fundamento.*?)? o prazo|assinar(?:, com fundamento.*?)? o prazo"),
    ("recomendacao",  r"recomendar"),
    ("ciencia",       r"dar ci[êe]ncia|cientificar|informar|comunicar|encaminhar c[óo]pia|remeter c[óo]pia|enviar c[óo]pia|dar conhecimento"),
    ("responsabilizacao", r"aplicar (?:a )?multa|ouvir em audi[êe]ncia|promover a audi[êe]ncia|citar|julgar irregulares|inabilitar"),
    ("cautelar",      r"(?:adotar|conceder|deferir|referendar|n[ãa]o referendar|revogar|indeferir)[^;]{0,60}cautelar|suspender cautelarmente"),
]
RX_TIPOS = [(t, re.compile(r"^\s*(?:e\s+)?(?:" + p + r")\b", re.I)) for t, p in TIPOS]

MPO = re.compile(
    r"Minist[ée]rio do Planejamento e Or[çc]amento|\bMPO\b|Secretaria de Or[çc]amento Federal|\bSOF\b|"
    r"Secretaria Nacional de Planejamento|\bSEPLAN\b|Secretaria de Monitoramento e Avalia[çc][ãa]o|\bSMA\b|"
    r"Minist[ée]rio da Economia", re.I)

RX_ITEM = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){1,3})\.?\s+(.+)$")
RX_PRAZO = re.compile(
    r"(?:no|em) prazo (?:m[áa]ximo )?de (?:at[ée] )?(\d+|[a-zçãé]+(?: e [a-zçãé]+)*) \(?\d*\)?\s*(dias?(?: [úu]teis| corridos)?|meses?|anos?)|"
    r"(PLOA(?:\s*\d{4})?|Projeto de Lei Or[çc]ament[áa]ria Anual[^,;.]{0,20}\d{4})|"
    r"(at[ée] \d{1,2}/\d{1,2}/\d{4})", re.I)
RX_DEST = re.compile(r"^\s*(?:e\s+)?\S+(?:\s+(?:ci[êe]ncia|c[óo]pia|conhecimento|alerta))?\s+(?:deste ac[óo]rd[ãa]o[^,]*?\s+)?"
                     r"(?:a[oa]s?|à|às)\s+(.+?)(?:,\s*(?:com fundamento|em articula|nos termos|para que|que\b|a fim)|\s+que\b|;|$)", re.I)


def secao_acordao(texto: str) -> str:
    m = re.search(r"^## Ac[óo]rd[ãa]o\s*$(.*?)(?=^## |\Z)", texto, re.M | re.S)
    return m.group(1) if m else ""


INTERNA = re.compile(r"^\s*(?:Unidade de Auditoria|Aud[A-ZÁ]|Secex|Seproc|Segecex|Secretaria-Geral|Serur|Sec\w+ do TCU)", re.I)


def verbo(corpo: str) -> str:
    """Trecho a partir do verbo: pula abertura como 'com fundamento no art. X, ...'."""
    if re.match(r"\s*(com fundamento|nos termos|com base|com amparo|com espeque|ante|diante)", corpo, re.I):
        for m in re.finditer(r",\s*", corpo[:400]):
            resto = corpo[m.end():]
            if any(rx.search(resto) for _, rx in RX_TIPOS) or re.match(r"(conhecer|negar|dar provimento|acolher|rejeitar|arquivar|encerrar|restituir|considerar)", resto, re.I):
                return resto
    return corpo


def classificar(corpo: str) -> str:
    for t, rx in RX_TIPOS:
        if rx.search(corpo):
            return t
    return "decisao"


def resumo(corpo: str, tipo: str, n: int = 180) -> str:
    """O comando em si: o que vem depois do 'que' (sem destinatário, base legal e prazo)."""
    c = re.sub(r"\s+", " ", corpo).strip()
    nucleo = None
    if tipo in ("determinacao", "recomendacao", "alerta", "cautelar"):
        m = re.search(r"\bque,?\s+(?:no prazo de [^,]+,\s*|em at[ée] [^,]+,\s*|observad[ao]s? [^,]+,\s*)?(.+)", c)
        if m and m.start() < 700:
            nucleo = m.group(1)
        elif tipo == "alerta":
            m = re.search(r"\b(?:sobre|acerca d[eao]s?|quanto a[os]?)\s+(.+)", c)
            nucleo = m.group(0) if m else None
    elif tipo == "ciencia":
        m = re.search(r"\b(?:a[oa]s?|à|às)\s+(.+?)(?:;|$)", c)
        nucleo = "para " + m.group(1) if m else None
    c = nucleo or c
    c = re.sub(r",?\s*com fundamento n[oa]s? .+?(?=,)", "", c)
    c = c[:1].upper() + c[1:]
    return c if len(c) <= n else c[:n].rsplit(" ", 1)[0] + "…"


def prazo(corpo: str) -> str | None:
    m = RX_PRAZO.search(corpo)
    if not m:
        return None
    if m.group(1):
        return f"{m.group(1)} {m.group(2)}"
    return (m.group(3) or m.group(4)).strip()


CABECALHO = re.compile(r"^(Unidade T[ée]cnica|Representa[çc][ãa]o legal|Representante|Relator|[ÓO]rg[ãa]o/Entidade|"
                       r"Interessad|Respons[áa]ve|Apens|Classe|Assunto|Natureza|Unidade Jurisdicionada|Recorrente|"
                       r"Embargante|Advogad|Processo)\b[^:]{0,40}:", re.I)
GRUPO = re.compile(r"^(Determina[çc][õo]es|Recomenda[çc][õo]es|Orienta[çc][õo]es|Ci[êe]ncia|Alertas?)\b[^:]{0,60}:\s*(.*)$", re.I)


def itens(texto_acordao: str) -> list[dict]:
    out: list[dict] = []
    grupo = None  # prefixo "1.7" quando o item é o bloco "Determinações/Recomendações/Orientações:" (acórdãos de relação)
    for linha in texto_acordao.splitlines():
        m = RX_ITEM.match(linha)
        nivel = m.group(1).count(".") if m else 0
        if m and nivel == 1:
            g = GRUPO.match(m.group(2).strip())
            if g:
                grupo = m.group(1)
                if not g.group(2) or re.match(r"n[ãa]o h[áa]", g.group(2), re.I):
                    continue
                out.append({"item": m.group(1), "texto": g.group(2), "sub": []})
                continue
            grupo = None
            out.append({"item": m.group(1), "texto": m.group(2).strip(), "sub": []})
        elif m and nivel == 2 and grupo and m.group(1).startswith(grupo + "."):
            out.append({"item": m.group(1), "texto": m.group(2).strip(), "sub": []})
        elif out and linha.strip():                    # subitens e continuações ficam no item pai
            out[-1]["sub"].append(linha.strip())
    out = [it for it in out if not CABECALHO.match(it["texto"])]
    res = []
    for it in out:
        corpo = verbo(it["texto"])
        completo = "\n".join([it["texto"]] + it["sub"])
        tipo = classificar(corpo)
        d = RX_DEST.match(corpo)
        dest = re.sub(r"\s+", " ", d.group(1)).strip(" ,") if d and tipo != "decisao" else None
        if tipo == "determinacao" and (not dest and re.search(r"determinar que se|determinar o (?:monitoramento|arquivamento|apensamento)", corpo, re.I)
                                       or dest and INTERNA.search(dest)):
            tipo = "decisao"  # providência interna do TCU (monitoramento, envio à unidade técnica)
        res.append({
            "item": it["item"], "tipo": tipo,
            "destinatario": dest[:220] if dest else None,
            "mpo": bool(dest and MPO.search(dest)),      # dirigido ao MPO
            "mpo_mencao": bool(MPO.search(completo)),     # MPO citado no item (articulação, cópia etc.)
            "prazo": prazo(completo) if tipo in ("determinacao", "responsabilizacao", "cautelar") else None,
            "resumo": resumo(corpo, tipo),
            "texto": completo,
        })
    return res


def main() -> int:
    if not os.path.exists(INDICE):
        print("sem acordaos/indice.json; nada a fazer")
        return 0
    indice = json.load(open(INDICE, encoding="utf-8"))
    por_proc: dict[str, list] = {}
    for a in indice.get("acordaos", []):
        caminho = os.path.join(PASTA, a["arquivo"])
        if not os.path.exists(caminho):
            continue
        its = itens(secao_acordao(open(caminho, encoding="utf-8").read()))
        por_proc.setdefault(a["processo"], []).append({
            "acordao": a["acordao"], "colegiado": a.get("colegiado"), "data_sessao": a.get("data_sessao"),
            "relator": a.get("relator"), "arquivo": a["arquivo"], "itens": its,
        })
    for lst in por_proc.values():  # mais recente primeiro
        lst.sort(key=lambda x: (x["data_sessao"] or "")[-4:] + (x["data_sessao"] or "")[3:5] + (x["data_sessao"] or "")[:2], reverse=True)
    saida = {"gerado_em": indice.get("atualizado_em"), "processos": por_proc}
    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(a["itens"]) for l in por_proc.values() for a in l)
    print(f"deliberacoes.json: {len(por_proc)} processos, {n} itens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
