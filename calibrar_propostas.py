#!/usr/bin/env python3
"""
calibrar_propostas.py — compara a PROPOSTA DE ENCAMINHAMENTO da unidade técnica
(transcrita no relatório do acórdão) com o que o Plenário decidiu.

Mede, para as medidas que alcançam o MPO, quantas propostas viraram deliberação
do mesmo tipo, quantas mudaram de tipo (ex.: determinação → recomendação) e
quantas caíram. Serve para calibrar o desconto da fase "proposta" na
metodologia de risco. Usa só texto público já baixado em acordaos/.

Saída: privado/calibragem_propostas.csv (item a item) e um resumo no terminal.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from extrair_deliberacoes import RX_TIPOS, MPO, secao_acordao  # noqa: E402

PASTA = os.path.join(AQUI, "acordaos")
INDICE = os.path.join(PASTA, "indice.json")
SAIDA = os.path.join(AQUI, "privado", "calibragem_propostas.csv")

RX_INICIO = re.compile(r"(proposta de encaminhamento|seguinte proposta|propondo|prop[õo]e-se|submete[m]?-se os autos)[^\n]{0,120}:\s*$", re.I | re.M)
RX_FIM = re.compile(r"^\s*(É o relat[óo]rio|[ÉE] o Relat[óo]rio|VOTO|Parecer do Minist[ée]rio P[úu]blico)", re.M)
RX_LETRA = re.compile(r"^\s*([a-z](?:\.\d{1,2}){0,4}|[ivx]{1,5}|\d{1,2}(?:\.\d{1,2}){0,4})\s*[\).–-]\s+(.+)$")
RX_BULLET = re.compile(r"^\s*[-•–]\s+(.+)$")
RX_VERBO = re.compile(r"^\s*(?:com fundamento[^,]{0,120},\s*)?(determinar|recomendar|dar ci[êe]ncia|dar conhecimento|cientificar|alertar|encaminhar c[óo]pia|enviar c[óo]pia|remeter c[óo]pia|informar|conhecer|arquivar|encerrar|restituir|autorizar|considerar|fixar|assinar|aplicar|ouvir|promover|adotar|expedir|comunicar)", re.I)
RX_AC = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){1,4})\.?\s+(.+)$")
RX_DEST = re.compile(r"^\s*(?:(?:determinar|recomendar|dar ci[êe]ncia|alertar|cientificar)\w*\s*,?\s*(?:com fundamento[^,]*,\s*)?)?(?:a[oa]s?|à|às)\s+(.+?)(?:,|\s+que\b|:|$)", re.I)
TIPO_ORD = {"cautelar": 5, "responsabilizacao": 5, "determinacao": 4, "recomendacao": 2, "alerta": 1, "ciencia": 1, "decisao": 0}
STOP = set("para pela pelo como seus suas este esta deste desta mais pelos pelas sobre entre quando onde cujo cuja qual quais "
           "prazo dias tribunal ministerio forma nesse nessa desse dessa assim ainda tambem sendo sejam seja".split())


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def tokens(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", norm(t)) if w not in STOP}


def tipo_de(txt: str) -> str | None:
    for t, rx in RX_TIPOS:
        if rx.search(txt):
            return t
    if re.match(r"\s*(conhecer|arquivar|encerrar|restituir|autorizar|considerar|sobrestar|apensar|encaminhar os autos)", txt, re.I):
        return "decisao"
    return None


def arvore(linhas: list[tuple[str, str]]) -> list[dict]:
    """Lista de itens (id, texto) -> folhas com tipo, destinatário e texto herdados dos ancestrais."""
    por_id = {i: t for i, t in linhas}
    ids = [i for i, _ in linhas]
    folhas = []
    for i, t in linhas:
        if any(o != i and o.startswith(i + ".") for o in ids):
            continue  # tem filhos: não é folha
        cadeia = [i]
        p = i
        while "." in p:
            p = p.rsplit(".", 1)[0]
            cadeia.insert(0, p)
        textos = [por_id.get(c, "") for c in cadeia]
        tipo = next((tp for tp in (tipo_de(x) for x in reversed(textos)) if tp), None)
        dest = None
        for x in reversed(textos):
            m = RX_DEST.match(x)
            if m and not re.match(r"\s*(em articula|no prazo|com fundamento)", m.group(1), re.I):
                dest = m.group(1); break
        folhas.append({"id": i, "tipo": tipo or "decisao", "dest": dest or "", "texto": " ".join(textos),
                       "mpo": bool(dest and MPO.search(dest)), "mpo_mencao": bool(MPO.search(t))})
    return folhas


def parse_bloco(bloco: str) -> list[tuple[str, str]]:
    linhas, nb = [], 0
    for ln in bloco.splitlines():
        mm = RX_LETRA.match(ln)
        mb = RX_BULLET.match(ln)
        if mm and (not mm.group(1).isdigit() or RX_VERBO.match(mm.group(2))):
            linhas.append((mm.group(1), mm.group(2).strip()))
        elif mb and RX_VERBO.match(mb.group(1)):
            nb += 1; linhas.append((f"b{nb}", mb.group(1).strip()))
        elif RX_VERBO.match(ln) and len(ln) > 30 and (not linhas or linhas[-1][1].rstrip().endswith((";", ".", ":"))):
            nb += 1; linhas.append((f"p{nb}", ln.strip()))
        elif linhas and ln.strip() and not re.match(r"^\s*\d+\.\s+[A-ZÁÉÍÓÚ]", ln):
            i, t = linhas[-1]; linhas[-1] = (i, t + " " + ln.strip())
    return linhas


def proposta(texto_md: str) -> list[dict]:
    """Escolhe, entre os blocos 'proposta de encaminhamento' do relatório, o que tem mais medidas."""
    m = re.search(r"^## Relat[óo]rio\s*$(.*?)(?=^## |\Z)", texto_md, re.M | re.S)
    rel = m.group(1) if m else ""
    melhor: list[dict] = []
    for ini in RX_INICIO.finditer(rel):
        bloco = rel[ini.end():ini.end() + 60000]
        f = RX_FIM.search(bloco)
        bloco = bloco[: f.start()] if f else bloco
        folhas = arvore(parse_bloco(bloco))
        if sum(1 for x in folhas if x["tipo"] != "decisao") >= sum(1 for x in melhor if x["tipo"] != "decisao"):
            melhor = folhas
    return melhor


def acordao_unidades(texto_md: str) -> list[dict]:
    linhas = []
    for ln in secao_acordao(texto_md).splitlines():
        mm = RX_AC.match(ln)
        if mm:
            linhas.append((mm.group(1), mm.group(2).strip()))
        elif linhas and ln.strip():
            i, t = linhas[-1]; linhas[-1] = (i, t + " " + ln.strip())
    unidades = arvore(linhas)
    # também os itens de nível superior inteiros (quando a proposta junta o que o acórdão separou)
    for i, t in linhas:
        if i.count(".") == 1:
            sub = " ".join(tt for ii, tt in linhas if ii == i or ii.startswith(i + "."))
            unidades.append({"id": i, "tipo": tipo_de(t) or "decisao", "texto": sub, "dest": "", "mpo": False, "mpo_mencao": bool(MPO.search(sub))})
    return unidades


def main() -> int:
    idx = json.load(open(INDICE, encoding="utf-8"))
    linhas_saida, resumo, por_proc = [], Counter(), Counter()
    acrescidas = 0
    for a in idx["acordaos"]:
        caminho = os.path.join(PASTA, a["arquivo"])
        md = open(caminho, encoding="utf-8").read()
        props = [p for p in proposta(md) if p["tipo"] != "decisao"]
        if not props:
            continue
        unis = acordao_unidades(md)
        usados = set()
        import math
        docs = [tokens(u["texto"]) for u in unis] + [tokens(p["texto"]) for p in props]
        df = Counter(w for d in docs for w in d)
        idf = {w: math.log((1 + len(docs)) / (1 + n)) + 1 for w, n in df.items()}
        for p in props:
            if not (p["mpo"] or p["mpo_mencao"]):
                continue
            tp = tokens(p["texto"])
            melhor, sc = None, 0.0
            for u in unis:
                tu = tokens(u["texto"])
                if not tp or not tu:
                    continue
                s = sum(idf[w] for w in tp & tu) / sum(idf[w] for w in tp)
                if s > sc:
                    melhor, sc = u, s
            if melhor and sc >= 0.40:
                usados.add(melhor["id"].split(".")[0] + "." + melhor["id"].split(".")[1])
                dt = TIPO_ORD.get(melhor["tipo"], 0) - TIPO_ORD.get(p["tipo"], 0)
                res = "mantida" if melhor["tipo"] == p["tipo"] else ("atenuada" if dt < 0 else "agravada")
            else:
                res = "suprimida"
            resumo[(p["tipo"], res)] += 1
            por_proc[(a["processo"], res)] += 1
            linhas_saida.append({"tc": a["processo"], "acordao": a["acordao"], "proposta": p["id"], "tipo_proposto": p["tipo"],
                                 "ao_mpo": "sim" if p["mpo"] else "articulação/cita", "resultado": res,
                                 "item_acordao": melhor["id"] if melhor and res != "suprimida" else "",
                                 "tipo_decidido": melhor["tipo"] if melhor and res != "suprimida" else "",
                                 "similaridade": f"{sc:.2f}", "texto_proposta": re.sub(r"\s+", " ", p["texto"])[:220]})
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas_saida[0].keys()), delimiter=";")
        w.writeheader(); w.writerows(linhas_saida)
    print(f"{len(linhas_saida)} propostas que alcançam o MPO, em {len({l['tc'] for l in linhas_saida})} processos")
    for tipo in ("determinacao", "recomendacao", "alerta", "ciencia"):
        tot = sum(v for (t, r), v in resumo.items() if t == tipo)
        if tot:
            print(f"  {tipo:13s} {tot:3d}: " + ", ".join(f"{r} {resumo[(tipo, r)]} ({resumo[(tipo, r)]*100//tot}%)" for r in ("mantida", "atenuada", "agravada", "suprimida") if resumo[(tipo, r)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
