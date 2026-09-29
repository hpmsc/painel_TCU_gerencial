#!/usr/bin/env python3
"""
baixar_acordaos.py — guarda o INTEIRO TEOR público dos acórdãos (acórdão,
relatório e voto) de cada processo acompanhado pelo painel.

Por quê: a análise de risco precisa ler relatório e voto na íntegra. Esses
textos são públicos na Pesquisa Integrada do TCU depois do julgamento. O
robô do GitHub já alcança o TCU; ao salvar os textos no repositório, eles
ficam disponíveis, completos e literais, para leitura e busca.

Só entra aqui o que o próprio TCU publica. Nenhuma peça sigilosa, nenhuma nota
interna.

Saída:
  acordaos/indice.json                          -> um registro por acórdão
  acordaos/<NNNNNNAAAAD>/<AAAA>-<NNNN>-<col>.md -> texto integral, em seções

Uso:
  python baixar_acordaos.py            # processos novos no índice ou com movimento nos últimos 10 dias
  python baixar_acordaos.py --todos    # varre todos os processos (usado às segundas)
  python baixar_acordaos.py --processo 025.632/2024-8
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

AQUI = os.path.dirname(os.path.abspath(__file__))
DADOS = os.path.join(AQUI, "site", "dados.json")
PASTA = os.path.join(AQUI, "acordaos")
INDICE = os.path.join(PASTA, "indice.json")

URL = "https://pesquisa.apps.tcu.gov.br/rest/publico/base/acordao-completo/documento"
PAUSA = 1.0          # segundos entre consultas, para não pesar no servidor do TCU
POR_PAGINA = 20
DIAS_RECENTES = 10

# Ordem das seções no arquivo .md. Campos não listados, se vierem, vão ao final.
SECOES = [
    ("SUMARIO", "Sumário"),
    ("ACORDAO", "Acórdão"),
    ("VOTO", "Voto"),
    ("DECLARACAOVOTO", "Declaração de voto"),
    ("VOTOCOMPLEMENTAR", "Voto complementar"),
    ("RELATORIO", "Relatório"),
]
ROTULOS = {"NUMACORDAO": "Número", "ANOACORDAO": "Ano", "COLEGIADO": "Colegiado",
           "DATASESSAO": "Sessão", "NUMATA": "Ata", "RELATOR": "Relator", "ASSUNTO": "Assunto",
           "ENTIDADE": "Órgãos/entidades", "INTERESSADOS": "Interessados", "QUORUM": "Quórum",
           "KEY": "Chave na Pesquisa Integrada"}
METADADOS = ["TITULO", "NUMACORDAO", "ANOACORDAO", "COLEGIADO", "DATASESSAO", "NUMATA",
             "RELATOR", "ASSUNTO", "ENTIDADE", "INTERESSADOS", "QUORUM", "KEY"]
IGNORAR = {"FRAGMENTOSINTEIROTEOR", "FAVORITO", "TIPO", "PROC"}

NATUREZAS_FORA = re.compile(r"aposentadoria|pens[ãa]o|reforma|admiss[ãa]o", re.I)
RX_NUM = re.compile(r"\d{3}\.\d{3}/\d{4}-\d")


def sessao() -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), raise_on_status=False)))
    s.headers.update({"Accept": "application/json",
                      "Referer": "https://pesquisa.apps.tcu.gov.br/",
                      "User-Agent": "painel-mpo/2.0 (monitoramento de dados abertos)"})
    return s


def texto_limpo(valor) -> str:
    """HTML do TCU -> texto simples com parágrafos."""
    if valor is None:
        return ""
    if isinstance(valor, list):
        valor = "\n".join(str(v) for v in valor)
    t = str(valor)
    t = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", t)
    t = re.sub(r"(?i)</?\s*(p|div|li|tr|h\d)(\s[^>]*)?>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()


def slug(numero: str) -> str:
    return re.sub(r"\D", "", numero)


def buscar(s: requests.Session, numero: str) -> list[dict]:
    """Todos os acórdãos cujo campo PROC é o processo (inclui recursos e embargos)."""
    achados, inicio = [], 0
    while True:
        r = s.get(URL, params={"termo": f'"{numero}"', "ordenacao": "DTRELEVANCIA desc",
                               "quantidade": POR_PAGINA, "inicio": inicio}, timeout=90)
        r.raise_for_status()
        d = r.json()
        docs = d.get("documentos") or []
        for doc in docs:
            proc = texto_limpo(doc.get("PROC"))
            if numero in RX_NUM.findall(proc):
                achados.append(doc)
        total = int(d.get("quantidadeEncontrada") or 0)
        inicio += POR_PAGINA
        if not docs or inicio >= total or inicio >= 200:
            return achados
        time.sleep(PAUSA)


def salvar(numero: str, doc: dict) -> dict:
    ano, num = str(doc.get("ANOACORDAO", "")).strip(), str(doc.get("NUMACORDAO", "")).strip()
    col = texto_limpo(doc.get("COLEGIADO"))
    col_curto = {"Plenário": "PL", "Primeira Câmara": "1C", "Segunda Câmara": "2C"}.get(col, re.sub(r"\W", "", col)[:6] or "X")
    rel = os.path.join(slug(numero), f"{ano}-{num.zfill(4)}-{col_curto}.md")
    destino = os.path.join(PASTA, rel)
    os.makedirs(os.path.dirname(destino), exist_ok=True)

    linhas = [f"# {texto_limpo(doc.get('TITULO')) or f'Acórdão {num}/{ano}'}", "",
              f"- **Processo:** {numero}"]
    for k in METADADOS:
        if k in ("TITULO",):
            continue
        v = texto_limpo(doc.get(k))
        if v:
            linhas.append(f"- **{ROTULOS.get(k, k.title())}:** {v.replace(chr(10), ' ')}")
    linhas.append(f"- **Fonte:** Pesquisa Integrada do TCU ({URL})")
    tamanhos = {}
    vistos = set(METADADOS) | IGNORAR
    for k, titulo in SECOES + [(k, k.title()) for k in sorted(doc) if k not in {c for c, _ in SECOES}]:
        if k in vistos or k not in doc:
            continue
        vistos.add(k)
        v = texto_limpo(doc.get(k))
        if not v:
            continue
        tamanhos[k] = len(v)
        linhas += ["", f"## {titulo}", "", v]
    conteudo = "\n".join(linhas) + "\n"
    antigo = open(destino, encoding="utf-8").read() if os.path.exists(destino) else None
    if antigo != conteudo:  # só reescreve se o TCU mudou o texto (evita commits vazios)
        with open(destino, "w", encoding="utf-8") as f:
            f.write(conteudo)
    return {
        "processo": numero, "acordao": f"{num}/{ano}", "colegiado": col,
        "data_sessao": texto_limpo(doc.get("DATASESSAO")), "relator": texto_limpo(doc.get("RELATOR")),
        "titulo": texto_limpo(doc.get("TITULO")), "chave": doc.get("KEY"),
        "arquivo": rel.replace(os.sep, "/"), "secoes": tamanhos,
        "sha1": hashlib.sha1(conteudo.encode()).hexdigest()[:12],
    }


def escolher(processos: list[dict], indice: dict, todos: bool, so: str | None) -> list[str]:
    if so:
        return [so]
    limite = (date.today() - timedelta(days=DIAS_RECENTES)).isoformat()
    ja = {a["processo"] for a in indice.get("acordaos", [])}
    consultados = indice.get("consultados", {})
    out = []
    for p in processos:
        if NATUREZAS_FORA.search(p.get("natureza") or ""):
            continue
        n = p["numero"]
        recente = any((m.get("data") or "")[:10] >= limite for m in p.get("movimentacoes") or [])
        if todos or n not in consultados or recente or (p.get("acordao") and n not in ja):
            out.append(n)
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--todos", action="store_true")
    ap.add_argument("--processo")
    ap.add_argument("--max-minutos", type=float, default=12,
                    help="para com segurança ao atingir o tempo; o resto fica para a próxima execução")
    a = ap.parse_args()

    dados = json.load(open(DADOS, encoding="utf-8"))
    indice = json.load(open(INDICE, encoding="utf-8")) if os.path.exists(INDICE) else {"acordaos": [], "consultados": {}}
    alvo = escolher(dados.get("processos", []), indice, a.todos, a.processo)
    alvo = list(alvo)
    print(f"{len(alvo)} processo(s) a consultar")

    por_chave = {x["chave"]: x for x in indice["acordaos"] if x.get("chave")}
    s, erros, novos = sessao(), [], 0
    fim = time.monotonic() + a.max_minutos * 60
    # Os mais antigos na fila primeiro, para que um corte por tempo não pule sempre os mesmos.
    alvo.sort(key=lambda n: indice.get("consultados", {}).get(n, ""))
    for i, numero in enumerate(alvo, 1):
        if time.monotonic() > fim:
            print(f"tempo esgotado: {len(alvo) - i + 1} processo(s) ficam para a próxima execução")
            break
        try:
            docs = buscar(s, numero)
        except Exception as e:  # uma falha não derruba o resto
            erros.append(f"{numero}: {e}")
            print(f"  [{i}/{len(alvo)}] {numero}: ERRO {e}", file=sys.stderr)
            continue
        for doc in docs:
            reg = salvar(numero, doc)
            antes = por_chave.get(reg["chave"] or reg["arquivo"])
            reg["baixado_em"] = antes.get("baixado_em") if antes else date.today().isoformat()
            if not antes:
                novos += 1
            por_chave[reg["chave"] or reg["arquivo"]] = reg
        indice.setdefault("consultados", {})[numero] = date.today().isoformat()
        print(f"  [{i}/{len(alvo)}] {numero}: {len(docs)} acórdão(s)")
        if i % 10 == 0:
            gravar(indice, por_chave, erros)
        time.sleep(PAUSA)

    gravar(indice, por_chave, erros)
    print(f"índice: {len(indice['acordaos'])} acórdãos ({novos} novos), {len(erros)} erro(s)")
    return 0


def gravar(indice: dict, por_chave: dict, erros: list) -> None:
    indice["acordaos"] = sorted(por_chave.values(), key=lambda x: (x["processo"], x["acordao"]))
    indice["atualizado_em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    indice["erros_ultima_execucao"] = erros
    os.makedirs(PASTA, exist_ok=True)
    with open(INDICE, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
