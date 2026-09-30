#!/usr/bin/env python3
"""
gerar_kanban.py — monta site/kanban.json para a página de etapas e prazos.

Usa SOMENTE dados públicos já coletados pelo coleta.py (site/dados.json) e o
histórico desse arquivo no git. Nada interno entra aqui.

O que faz:
  1. Percorre todas as versões de site/dados.json no histórico do git e junta
     as movimentações de cada processo numa linha do tempo única (a coleta
     diária só guarda uma janela recente; o histórico recupera o resto).
  2. Classifica cada movimentação: se ela indica mudança de ETAPA (instrução,
     relator, pauta, julgado, recurso, encerrado) ou um ato de COMUNICAÇÃO
     (ciência, resposta, encerramento de ciclo).
  3. Calcula os prazos a partir da ciência registrada por órgão do MPO.

Regra de prazo (versão de teste):
  - O prazo começa no dia útil em que o destinatário registra a ciência
    (se a ciência cair em fim de semana/feriado, vale o dia útil seguinte).
  - Conta em dias corridos, excluindo o dia do início e incluindo o do
    vencimento; se o vencimento cair em dia não útil, passa para o próximo
    dia útil.
  - Quantidade de dias: PRAZO_PADRAO_DIAS, salvo se prazos.txt disser outra.
  - A comunicação deixa de contar quando o TCU registra a resposta daquele
    documento ou encerra o ciclo de comunicação do processo.

Uso:
    python gerar_kanban.py                 # lê o git e site/dados.json
    python gerar_kanban.py --sem-historico # só o dados.json atual
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
DADOS = os.path.join(AQUI, "site", "dados.json")
SAIDA = os.path.join(AQUI, "site", "kanban.json")
PRAZOS_TXT = os.path.join(AQUI, "prazos.txt")

PRAZO_PADRAO_DIAS = 15  # teste: prazo presumido quando prazos.txt não informa

# Feriados nacionais (dias não úteis para início e vencimento de prazo).
FERIADOS = {
    # 2026
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-04-03", "2026-04-21",
    "2026-05-01", "2026-06-04", "2026-09-07", "2026-10-12", "2026-11-02",
    "2026-11-15", "2026-11-20", "2026-12-25",
    # 2027
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-26", "2027-04-21",
    "2027-05-01", "2027-05-27", "2027-09-07", "2027-10-12", "2027-11-02",
    "2027-11-15", "2027-11-20", "2027-12-25",
}

# Destinatários que contam como "o MPO tomou ciência" (texto após "em nome de").
DESTINATARIO_MPO = re.compile(
    r"Planejamento e Or[çc]amento|Secretaria de Or[çc]amento Federal|"
    r"Secretaria de Monitoramento e Avalia|Secretaria Nacional de Planejamento|"
    r"Assessoria Especial de Controle Interno", re.I)

ETAPAS = ["instrucao", "relator", "pauta", "julgado", "recurso", "encerrado"]

# Ordem importa: a primeira regra que casar define a etapa do evento.
REGRAS_ETAPA: list[tuple[str, re.Pattern]] = [
    ("encerrado", re.compile(r"^Processo encerrado", re.I)),
    # Acórdão que julga recurso também é "julgado": o recurso foi decidido.
    ("julgado",   re.compile(r"^Ac[óo]rd[ãa]o \d+|^Apreciado na Sess[ãa]o", re.I)),
    ("recurso",   re.compile(r"/R\d{3}\b|AudRecursos|Serur|por meio de recurso", re.I)),
    ("relator",   re.compile(r"exclu[íi]do da pauta", re.I)),
    ("pauta",     re.compile(r"inclu[íi]do na pauta|pedido de vista|enviado de MINS?-\S+ para Seses", re.I)),
    ("relator",   re.compile(r"Enviado para pronunciamento do Ministro|enviado de \S+ para MINS?-|"
                             r"Pronunciamento d[ao] \S+ conclu[íi]do", re.I)),
    ("instrucao", re.compile(r"enviado de \S+ para Aud|Unidade respons[áa]vel t[ée]cnica alterada|"
                             r"Portaria de Fiscaliza[çc][ãa]o", re.I)),
]

# --------------------------------------------------------------------------- #
# Trilha de atuação: uma só leitura do andamento, em 5 colunas, pensada para a
# ação do MPO. Junta a etapa (onde o processo está) e o marco do rito da
# auditoria (relatório preliminar, proposta concluída), que antes eram dois
# quadros separados.
TRILHA = [
    (1, "Fiscalização em curso", "portaria, requisições, diligências, instrução"),
    (2, "Relatório preliminar", "comentários do gestor"),
    (3, "Proposta no relator", "instrução concluída, gabinete do ministro"),
    (4, "Pauta", "sessão marcada, vista"),
    (5, "Julgado e cumprimento", "acórdão, cumprimento, recurso"),
]
JANELA = {2, 3}  # onde ainda dá para influenciar o conteúdo da decisão

REGRAS_MARCO: list[tuple[int, re.Pattern]] = [
    (6, re.compile(r"^Ac[óo]rd[ãa]o \d|Apreciado na Sess", re.I)),
    (5, re.compile(r"inclu[íi]do na pauta|pedido de vista|enviado de MINS?-\S+ para Seses", re.I)),
    (4, re.compile(r"Pronunciamento d[ao] \S+ conclu|Enviado para pronunciamento do Ministro|enviado de \S+ para MINS?-", re.I)),
    (3, re.compile(r"coment[áa]rios dos gestores|Relat[óo]rio Preliminar|vers[ãa]o preliminar", re.I)),
    (2, re.compile(r"Of[íi]cio de Requisi|oitiva|dilig[êe]ncia|Registrada ci[êe]ncia|Elementos comprobat|via CONECTA", re.I)),
    (1, re.compile(r"Portaria de Fiscaliza|autuad", re.I)),
]


def coluna(etapa: str, marco: int | None) -> int:
    if etapa in ("julgado", "recurso", "encerrado"):
        return 5
    if etapa == "pauta":
        return 4
    if etapa == "relator":
        return 3
    return 2 if marco == 3 else 1


def marcar_trilha(eventos: list[dict], base: str) -> int:
    """Grava ev["col"] nos eventos em que o processo muda de coluna; devolve a coluna de partida."""
    etapa, marco = base, None
    atual = inicio = coluna(base, None)
    for ev in eventos:
        if ev.get("etapa"):
            etapa = ev["etapa"]
        for n, rx in REGRAS_MARCO:
            if rx.search(ev["desc"]):
                # Depois do acórdão, ciência, ofício e envio à Seses são rotina de
                # cumprimento: só portaria, relatório preliminar, proposta ou pauta reabrem.
                reabre = marco != 6 or n in (1, 3, 4) or (n == 5 and "seses" not in ev["desc"].lower())
                if reabre and (n == 6 or marco is None or marco == 6 or n > marco):
                    marco = n
                break
        c = coluna(etapa, marco)
        if c != atual:
            ev["col"] = atual = c
    return inicio


RX_DOC = r"((?:Of[íi]cio|Aviso|Edital|Notifica[çc][ãa]o)\s+[\d./]+(?:-TCU/[^\s.]+)?)"
RX_CIENCIA = re.compile(r"Registrada ci[êe]ncia de comunica[çc][ãa]o d[oa] " + RX_DOC, re.I)
RX_RESPOSTA = re.compile(r"Registrada resposta de comunica[çc][ãa]o d[oa] " + RX_DOC, re.I)
RX_ENCERRA_CICLO = re.compile(r"encerramento de ciclo de comunica[çc][ãa]o", re.I)
RX_EXPEDIDO = re.compile(r"Juntada comunica[çc][ãa]o " + RX_DOC + r".*expedi", re.I)
# Documento entregue pela parte depois da ciência: indica resposta, mesmo que o
# TCU não registre formalmente "resposta de comunicação".
RX_JUNTADA_PARTE = re.compile(r"juntado ao processo via CONECTA|^Documento Resposta|\(Resposta\b", re.I)
RX_EM_NOME = re.compile(r"em nome de (.+)$", re.I)
# Trâmites de rotina depois do acórdão (formalização na Seses, remessa da Seproc).
RX_POS_ACORDAO = re.compile(r"para Seses|enviado de Seproc para", re.I)


# --------------------------------------------------------------------------- #
def dia_util(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in FERIADOS


def proximo_util(d: date) -> date:
    while not dia_util(d):
        d += timedelta(days=1)
    return d


def vencimento(ciencia: date, dias: int) -> tuple[date, date]:
    inicio = proximo_util(ciencia)
    return inicio, proximo_util(inicio + timedelta(days=dias))


def chave_doc(doc: str) -> str:
    """'Ofício 0060/2026-TCU/AudGestãoInovação' -> 'oficio 60/2026' (para casar ciência×resposta)."""
    m = re.search(r"(Of[íi]cio|Aviso|Edital|Notifica)\D*0*(\d+)/(\d{4})", doc, re.I)
    if not m:
        return doc.lower()
    tipo = m.group(1).lower().replace("í", "i")[:5]
    return f"{tipo} {int(m.group(2))}/{m.group(3)}"


def carregar_prazos() -> tuple[dict, list]:
    """
    prazos.txt aceita três formas (campos separados por |):
      NNN.NNN/AAAA-D | dias                                 -> prazo de todas as ciências do processo
      NNN.NNN/AAAA-D | Ofício 123/2026 | dias               -> prazo daquele documento
      NNN.NNN/AAAA-D | Ofício 123/2026 | 08/09/2026 | dias  -> comunicação que a coleta não
                                                               capturou, com a data da ciência
    """
    regras: dict = {}
    manuais: list = []
    if not os.path.exists(PRAZOS_TXT):
        return regras, manuais
    for linha in open(PRAZOS_TXT, encoding="utf-8"):
        linha = linha.split("#", 1)[0].strip()
        if not linha:
            continue
        partes = [p.strip() for p in linha.split("|")]
        try:
            dias = int(re.sub(r"\D", "", partes[-1]))
        except ValueError:
            continue
        if len(partes) == 4:
            try:
                ciencia = datetime.strptime(partes[2], "%d/%m/%Y").date()
            except ValueError:
                continue
            manuais.append((partes[0], partes[1], ciencia, dias))
            continue
        doc = chave_doc(partes[1]) if len(partes) == 3 else "*"
        regras[(partes[0], doc)] = dias
    return regras, manuais


def classificar(desc: str) -> dict:
    ev: dict = {}
    for etapa, rx in REGRAS_ETAPA:
        if rx.search(desc):
            ev["etapa"] = etapa
            break
    if m := RX_CIENCIA.search(desc):
        ev["com"] = "ciencia"; ev["doc"] = m.group(1)
        if n := RX_EM_NOME.search(desc):
            ev["dest"] = n.group(1).strip()
    elif m := RX_RESPOSTA.search(desc):
        ev["com"] = "resposta"; ev["doc"] = m.group(1)
    elif RX_ENCERRA_CICLO.search(desc):
        ev["com"] = "encerra"
    elif m := RX_EXPEDIDO.search(desc):
        ev["com"] = "expedido"; ev["doc"] = m.group(1)
    elif RX_JUNTADA_PARTE.search(desc):
        ev["com"] = "juntada"
    return ev


# --------------------------------------------------------------------------- #
def versoes_git() -> list[dict]:
    try:
        saida = subprocess.check_output(
            ["git", "log", "--format=%H", "--", "site/dados.json"], cwd=AQUI, text=True)
    except Exception as e:  # sem git: segue só com o arquivo atual
        print(f"aviso: histórico do git indisponível ({e})", file=sys.stderr)
        return []
    versoes = []
    for h in reversed(saida.split()):
        try:
            bruto = subprocess.check_output(["git", "show", f"{h}:site/dados.json"], cwd=AQUI)
            d = json.loads(bruto)
        except Exception:
            continue
        if d.get("versao") == 2 and d.get("processos"):
            versoes.append(d)
    return versoes


def montar(versoes: list[dict]) -> dict:
    atual = json.load(open(DADOS, encoding="utf-8"))
    versoes = versoes + [atual]
    prazos_cfg, prazos_manuais = carregar_prazos()

    procs: dict[str, dict] = {}
    for v in versoes:
        dia = (v.get("gerado_em") or "")[:10]
        for p in v["processos"]:
            reg = procs.setdefault(p["numero"], {"movs": {}, "visto": []})
            reg["meta"] = p
            if dia and (not reg["visto"] or reg["visto"][-1] != dia):
                reg["visto"].append(dia)
            for m in p.get("movimentacoes") or []:
                reg["movs"][(m["data"][:10], m["descricao"])] = m

    abertos_hoje = {p["numero"] for p in atual["processos"]}
    hoje = date.fromisoformat(atual["gerado_em"][:10])
    saida = []
    for numero, reg in procs.items():
        p = reg["meta"]
        eventos = []
        for (dia, desc), m in sorted(reg["movs"].items()):
            ev = {"data": dia, "desc": desc, **classificar(desc)}
            if not ev.get("etapa") and (m.get("acordao") or m.get("fase") == "Julgado"):
                ev["etapa"] = "julgado"
            eventos.append(ev)
        # O TCU registra no mesmo dia o envio à Seses e o acórdão; a ordem alfabética
        # punha o envio à Seses depois do acórdão e o processo "voltava" para a pauta.
        # No mesmo dia, a etapa mais adiantada fica por último.
        eventos.sort(key=lambda ev: (ev["data"], ETAPAS.index(ev["etapa"]) if ev.get("etapa") else -1))
        # Depois do acórdão, a remessa da Seproc à unidade técnica é cumprimento, não
        # nova instrução: o processo continua "julgado" até um sinal de novo ciclo
        # (portaria de fiscalização, envio ao relator, pauta ou recurso).
        atual_et = None
        for ev in eventos:
            et = ev.get("etapa")
            if atual_et == "julgado" and et in ("pauta", "instrucao") and RX_POS_ACORDAO.search(ev["desc"]):
                ev.pop("etapa")
                continue
            if et:
                atual_et = et

        # Etapa de partida quando nenhuma movimentação define a etapa.
        base = "julgado" if p.get("acordao") else "instrucao"
        col_base = marcar_trilha(eventos, base)

        # Comunicações e prazos
        coms: dict[str, dict] = {}
        for ev in eventos:
            c = ev.get("com")
            if c == "ciencia":
                k = chave_doc(ev["doc"])
                dest = ev.get("dest")
                do_mpo = bool(dest and DESTINATARIO_MPO.search(dest)) or (
                    not dest and p.get("vinculo") in ("unidade", "acompanhado"))
                if not do_mpo:
                    continue
                dias = prazos_cfg.get((numero, k), prazos_cfg.get((numero, "*")))
                fonte = "prazos.txt" if dias else "presumido"
                dias = dias or PRAZO_PADRAO_DIAS
                ini, venc = vencimento(date.fromisoformat(ev["data"]), dias)
                coms[k] = {"doc": ev["doc"], "dest": dest or "destinatário não informado",
                           "ciencia": ev["data"], "inicio": ini.isoformat(), "dias": dias,
                           "fonte": fonte, "vencimento": venc.isoformat(),
                           "status": "aberto", "fechado_em": None}
            elif c == "resposta":
                k = chave_doc(ev["doc"])
                if k in coms and coms[k]["status"] == "aberto":
                    coms[k].update(status="respondido", fechado_em=ev["data"])
            elif c == "juntada":
                for x in coms.values():
                    if x["status"] == "aberto" and x["ciencia"] <= ev["data"]:
                        x.update(status="documento juntado", fechado_em=ev["data"])
            elif c == "encerra":
                for x in coms.values():
                    if x["status"] == "aberto" and x["ciencia"] <= ev["data"]:
                        x.update(status="ciclo encerrado", fechado_em=ev["data"])

        for num, doc, ciencia, dias in prazos_manuais:
            if num != numero or chave_doc(doc) in coms:
                continue
            ini, venc = vencimento(ciencia, dias)
            k = chave_doc(doc)
            coms[k] = {"doc": doc, "dest": "informado em prazos.txt", "ciencia": ciencia.isoformat(),
                       "inicio": ini.isoformat(), "dias": dias, "fonte": "prazos.txt",
                       "vencimento": venc.isoformat(), "status": "aberto", "fechado_em": None}
            for ev in eventos:  # uma resposta registrada depois fecha também o prazo manual
                if ev["data"] >= ciencia.isoformat() and (
                        (ev.get("com") == "resposta" and chave_doc(ev["doc"]) == k)
                        or ev.get("com") in ("juntada", "encerra")):
                    coms[k].update(status="respondido" if ev.get("com") == "resposta" else
                                   ("documento juntado" if ev["com"] == "juntada" else "ciclo encerrado"),
                                   fechado_em=ev["data"])
                    break

        # Ciclos já encerrados há muito tempo não interessam ao quadro.
        limite = (hoje - timedelta(days=120)).isoformat()
        comunicacoes = [c for c in coms.values() if c["status"] == "aberto" or c["vencimento"] >= limite]

        saida.append({
            "numero": numero,
            "assunto": p.get("assunto"),
            "natureza": p.get("natureza"),
            "relator": p.get("relator"),
            "unidade_tecnica": p.get("unidade_tecnica"),
            "orgaos": p.get("orgaos") or [],
            "vinculo": p.get("vinculo"),
            "acordao": p.get("acordao"),
            "url": p.get("url_push"),
            "aberto": numero in abertos_hoje,
            "primeiro_visto": reg["visto"][0] if reg["visto"] else None,
            "ultimo_visto": reg["visto"][-1] if reg["visto"] else None,
            "etapa_base": base,
            "col_base": col_base,
            "eventos": eventos,
            "comunicacoes": comunicacoes,
        })

    saida.sort(key=lambda x: x["numero"])
    return {
        "versao": 1,
        "gerado_em": atual.get("gerado_em"),
        "gerado_em_br": atual.get("gerado_em_br"),
        "hoje": hoje.isoformat(),
        "inicio_historico": min((x["primeiro_visto"] for x in saida if x["primeiro_visto"]), default=None),
        "prazo_padrao_dias": PRAZO_PADRAO_DIAS,
        "feriados": sorted(FERIADOS),
        "etapas": ETAPAS,
        "trilha": [{"n": n, "nome": nome, "sub": sub, "janela": n in JANELA} for n, nome, sub in TRILHA],
        "processos": saida,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-historico", action="store_true")
    ap.add_argument("--saida", default=SAIDA)
    a = ap.parse_args()
    versoes = [] if a.sem_historico else versoes_git()
    dados = montar(versoes)
    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))
    n = len(dados["processos"])
    abertos = sum(1 for c in (c for p in dados["processos"] for c in p["comunicacoes"]) if c["status"] == "aberto")
    print(f"kanban.json: {n} processos, {len(versoes)} versões do histórico, {abertos} comunicações em aberto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
