#!/usr/bin/env python3
"""
gerar_risco_privado.py — junta numa só fonte as notas de risco por deliberação
que a aba Deliberações mostra quando alguém carrega o arquivo privado.

Ordem de prevalência, item a item:
  1. revisão manual da SEAI   (privado/ajustes_risco.csv)
  2. leitura com a skill      (privado/leituras/resultado_*.json, modo lote)
  3. regra automática         (pontuar_risco.py, para itens não lidos)

TUDO AQUI É PRIVADO. Lê e grava só dentro de privado/, que o .gitignore mantém
fora do repositório. Este script não contém nenhuma nota nem justificativa.

Saída: privado/risco_deliberacoes.csv (formato que a aba Deliberações carrega).
Uso:   python gerar_risco_privado.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
PRIV = os.path.join(AQUI, "privado")
LEITURAS = os.path.join(PRIV, "leituras")
AJUSTES = os.path.join(PRIV, "ajustes_risco.csv")
SAIDA = os.path.join(PRIV, "risco_deliberacoes.csv")
DELIB = os.path.join(AQUI, "site", "deliberacoes.json")
AJ_FASE = {"vigente": 0, "proposta": -1, "potencial": -2, "latente": -1}
CAMPOS = ["tc", "acordao", "item", "tipo", "faixa", "faixa_latente", "F", "I", "dimensao", "fase",
          "origem", "prazo", "resumo", "justificativa"]


def faixa(f: int, i: int) -> int:
    p = f * i
    return 5 if (f == 5 and i >= 2) or p >= 13 else 4 if p >= 9 else 3 if p >= 6 else 2 if p >= 3 else 1


def norm_acordao(s: str) -> str:
    m = re.search(r"(\d{1,5})/(\d{4})", str(s))
    return f"{int(m.group(1))}/{m.group(2)}" if m else str(s).strip()


def norm_item(s: str) -> str:
    m = re.match(r"\s*(\d{1,2}(?:\.\d{1,2}){1,3})", str(s))
    return m.group(1) if m else str(s).strip()


def ler_csv(caminho: str) -> list[dict]:
    if not os.path.exists(caminho):
        return []
    with open(caminho, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))


def main() -> int:
    if not os.path.isdir(PRIV):
        print("sem pasta privado/: nada a fazer"); return 0
    validos = set()
    if os.path.exists(DELIB):
        for tc, acs in json.load(open(DELIB, encoding="utf-8"))["processos"].items():
            for a in acs:
                for it in a["itens"]:
                    validos.add((tc, a["acordao"], it["item"]))

    linhas: dict[tuple, dict] = {}
    # Processo lido com a skill: a leitura decide o que pontua (a regra automática não entra nele).
    lidos = set()
    for arq in glob.glob(os.path.join(LEITURAS, "resultado_*.json")):
        try:
            lidos.add(json.load(open(arq, encoding="utf-8"))["tc"])
        except (ValueError, KeyError):
            pass
    # 3. regra automática (base, só para processos não lidos)
    subprocess.run([sys.executable, os.path.join(AQUI, "pontuar_risco.py")], check=False, capture_output=True)
    for r in ler_csv(SAIDA):
        if r.get("origem") == "automática" and r["tc"] not in lidos:
            linhas[(r["tc"], r["acordao"], r["item"])] = {k: r.get(k, "") for k in CAMPOS}
    # 2. leitura com a skill
    lidas = 0
    for arq in sorted(glob.glob(os.path.join(LEITURAS, "resultado_*.json"))):
        r = json.load(open(arq, encoding="utf-8"))
        for it in r.get("itens", []):
            try:
                F, I = int(it.get("F")), int(it.get("I"))
            except (TypeError, ValueError):
                continue  # decisão processual: não pontua
            if "dilig" in str(it.get("tipo", "")).lower():
                continue
            k = (r["tc"], norm_acordao(it.get("acordao", "")), norm_item(it.get("item", "")))
            linhas[k] = {"tc": k[0], "acordao": k[1], "item": k[2], "tipo": it.get("tipo", ""),
                         "F": F, "I": I, "dimensao": it.get("dimensao", ""), "fase": it.get("fase") or "vigente",
                         "origem": "leitura", "prazo": it.get("prazo_est") or it.get("prazo_texto") or "",
                         "resumo": (it.get("comando") or "")[:160], "justificativa": it.get("conta", "")}
            lidas += 1
    # 1. revisão manual
    for a in ler_csv(AJUSTES):
        k = (a["tc"].strip(), norm_acordao(a["acordao"]), norm_item(a["item"]))
        base = linhas.get(k, {"tc": k[0], "acordao": k[1], "item": k[2], "tipo": a.get("tipo", ""),
                              "F": a.get("F") or 1, "I": a.get("I") or 1, "dimensao": "", "fase": "vigente",
                              "prazo": "", "resumo": ""})
        for campo in ("F", "I", "dimensao", "fase", "tipo"):
            if a.get(campo):
                base[campo] = a[campo]
        if a.get("justificativa"):
            base["justificativa"] = a["justificativa"]
        base["origem"] = "revisada"
        linhas[k] = base

    # Subitem lido separado (9.4.1) e exibido junto na aba (9.4): vale o pior subitem.
    if validos:
        for k in [k for k in linhas if k not in validos]:
            pai = (k[0], k[1], k[2].rsplit(".", 1)[0])
            if pai == k or pai not in validos:
                continue
            l = linhas.pop(k)
            atual = linhas.get(pai)
            if atual and atual.get("origem") == "revisada":
                continue
            if not atual or atual.get("origem") == "automática" or \
                    faixa(int(l["F"]), int(l["I"])) > faixa(int(atual["F"]), int(atual["I"])):
                l["item"] = pai[2]
                linhas[pai] = l
    saida, fora = [], 0
    for k, l in linhas.items():
        if l.get("fase") == "encerrada":
            continue
        F, I = int(l["F"]), int(l["I"])
        b = faixa(F, I)
        l.update(F=F, I=I, faixa=max(1, b + AJ_FASE.get(l.get("fase"), 0)),
                 faixa_latente=b if l.get("fase") == "latente" else "")
        if validos and k not in validos:
            fora += 1  # item que a aba não exibe (numeração diferente): fica no arquivo, sem efeito
        saida.append({c: l.get(c, "") for c in CAMPOS})
    saida.sort(key=lambda r: (-int(r["faixa"]), r["tc"], r["acordao"], r["item"]))
    with open(SAIDA, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, delimiter=";")
        w.writeheader(); w.writerows(saida)
    from collections import Counter
    c = Counter(r["origem"] for r in saida)
    print(f"{SAIDA}: {len(saida)} itens ({', '.join(f'{k} {v}' for k, v in c.items())}); "
          f"{lidas} vindos da leitura; {fora} sem correspondência na aba")
    return 0


if __name__ == "__main__":
    sys.exit(main())
