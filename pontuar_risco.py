#!/usr/bin/env python3
"""
pontuar_risco.py — nota de risco preliminar (1 a 5) para cada deliberação que
alcança o MPO, pela metodologia v0.2 (força × impacto, ajustada pela fase).

A SAÍDA É PRIVADA. O script grava em privado/risco_deliberacoes.csv, pasta que
o .gitignore mantém fora do repositório. A aba Deliberações só mostra as notas
quando alguém carrega esse arquivo no próprio navegador.

As regras abaixo são o ponto de partida automático. A secretaria revisa a nota
e registra ajustes em privado/ajustes_risco.csv (mesmas chaves), que prevalecem.

Uso:
  python pontuar_risco.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
DELIB = os.path.join(AQUI, "site", "deliberacoes.json")
PRIV = os.path.join(AQUI, "privado")
SAIDA = os.path.join(PRIV, "risco_deliberacoes.csv")
AJUSTES = os.path.join(PRIV, "ajustes_risco.csv")

NOME = {1: "Baixo", 2: "Moderado", 3: "Relevante", 4: "Alto", 5: "Crítico"}

# ---- Eixo 1: força da medida (pelo tipo e pelo texto) ----------------------
def forca(it: dict) -> int | None:
    t, txt = it["tipo"], it["texto"]
    if t in ("cautelar", "responsabilizacao"):
        return 5
    if t == "determinacao":
        if it.get("prazo") or re.search(r"plano de a[çc][ãa]o|no prazo|PLOA|Lei Or[çc]ament[áa]ria", txt, re.I):
            return 4
        return 3
    if t == "recomendacao":
        return 2
    if t in ("alerta", "ciencia"):
        return 1
    return None  # outras decisões (conhecer, arquivar…) não entram na escala

# ---- Eixo 2: impacto para o MPO (maior das quatro dimensões) ----------------
FISCAL_3 = re.compile(r"PLOA|Lei Or[çc]ament[áa]ria Anual|meta (?:de resultado )?fiscal|resultado prim[áa]rio|regras? fiscais|"
                      r"Lei Complementar 200|LC 200|Responsabilidade Fiscal|contingenciamento|limites? de despesas?|"
                      r"Conta [ÚU]nica|Or[çc]amento Geral da Uni[ãa]o|classifica[çc][ãa]o or[çc]ament|dota[çc][ãa]o|"
                      r"cr[ée]dito or[çc]ament|regime or[çc]ament|receitas? p[úu]blicas? n[ãa]o", re.I)
FISCAL_2 = re.compile(r"or[çc]ament|receita|despesa|fundo|transpar[êe]ncia fiscal|referencial monet|financeir|tribut", re.I)
ALCANCE_3 = re.compile(r"Poder Executivo|[óo]rg[ãa]os e entidades da administra|Administra[çc][ãa]o P[úu]blica Federal|"
                       r"todos os (?:[óo]rg[ãa]os|minist[ée]rios)|fundos com participa[çc][ãa]o da Uni[ãa]o", re.I)
ORGAOS = re.compile(r"Minist[ée]rio|Casa Civil|Controladoria|Tesouro|Banco Central|Caixa Econ|BNDES|Ag[êe]ncia|Secretaria Especial", re.I)
PESSOAL_3 = re.compile(r"audi[êe]ncia|multa|responsabiliza|inabilita", re.I)
OPER_3 = re.compile(r"projeto de lei|proposta legislativa|altera[çc][ãa]o (?:da|do|de) (?:Lei|art)|decreto|SIAFI|SIOP", re.I)
OPER_2 = re.compile(r"plano de|sistema|normativ|regulament|metodologia|mecanismo|portal|publica[çc][ãa]o peri[óo]dica", re.I)


def impacto(it: dict) -> tuple[int, str]:
    txt = it["texto"]
    dims = {
        "fiscal": 3 if FISCAL_3.search(txt) else 2 if FISCAL_2.search(txt) else 1,
        "alcance": 3 if ALCANCE_3.search(txt) else 2 if len(set(m.group(0).lower() for m in ORGAOS.finditer(txt))) >= 2 or len(ORGAOS.findall(txt)) >= 3 else 1,
        "pessoal": 3 if PESSOAL_3.search(txt) else 1,
        "operacional": 3 if OPER_3.search(txt) else 2 if OPER_2.search(txt) else 1,
    }
    dim = max(dims, key=lambda k: (dims[k], k == "fiscal"))
    return dims[dim], dim


def faixa(f: int, i: int) -> int:
    p = f * i
    if (f == 5 and i >= 2) or p >= 13:
        return 5
    return 4 if p >= 9 else 3 if p >= 6 else 2 if p >= 3 else 1


AJUSTE_FASE = {"vigente": 0, "proposta": -1, "potencial": -2, "latente": -1}


def carregar_ajustes() -> dict:
    if not os.path.exists(AJUSTES):
        return {}
    out = {}
    with open(AJUSTES, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            out[(r["tc"].strip(), r["acordao"].strip(), r["item"].strip())] = r
    return out


def main() -> int:
    d = json.load(open(DELIB, encoding="utf-8"))["processos"]
    ajustes = carregar_ajustes()
    linhas = []
    for tc, acs in d.items():
        for a in acs:
            for it in a["itens"]:
                alcanca = it.get("mpo") or (it.get("mpo_mencao") and it["tipo"] != "ciencia") or \
                          (it["tipo"] == "alerta" and re.search(r"Poder Executivo", it["texto"]))
                aj = ajustes.get((tc, a["acordao"], it["item"]))
                f = forca(it)
                if not aj and (not alcanca or f is None):
                    continue
                i, dim = impacto(it)
                f = f or 1
                fase, just, origem = "vigente", "", "automática"
                if aj:
                    f = int(aj.get("F") or f); i = int(aj.get("I") or i)
                    dim = aj.get("dimensao") or dim; fase = aj.get("fase") or fase
                    just = aj.get("justificativa", ""); origem = "revisada"
                base = faixa(f, i)
                if fase == "encerrada":
                    continue
                final = max(1, base + AJUSTE_FASE.get(fase, 0))
                linhas.append({
                    "tc": tc, "acordao": a["acordao"], "item": it["item"], "tipo": (aj or {}).get("tipo") or it["tipo"],
                    "faixa": final, "faixa_latente": base if fase == "latente" else "",
                    "F": f, "I": i, "dimensao": dim, "fase": fase, "origem": origem,
                    "prazo": it.get("prazo") or "", "resumo": it["resumo"][:160], "justificativa": just,
                })
    linhas.sort(key=lambda r: (-r["faixa"], r["tc"], r["acordao"], r["item"]))
    os.makedirs(PRIV, exist_ok=True)
    with open(SAIDA, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()) if linhas else ["tc"], delimiter=";")
        w.writeheader(); w.writerows(linhas)
    from collections import Counter
    c = Counter(r["faixa"] for r in linhas)
    print(f"{SAIDA}: {len(linhas)} deliberações · " + " · ".join(f"{k} {NOME[k]}: {c.get(k,0)}" for k in range(5, 0, -1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
