#!/usr/bin/env python3
"""
coletar_pauta.py — pauta das sessões colegiadas do TCU, filtrada para o MPO.

Monta o "monitoramento da pauta" que a SEAI recebe por e-mail toda semana:
  * as sessões desta semana (Plenário, 1ª e 2ª Câmaras) e, em cada uma, os
    processos que citam o MPO ou suas secretarias, ou que estão no painel;
  * os processos da pauta da semana passada e o que o TCU decidiu (acórdão).

Fontes, todas públicas:
  * lista de sessões: sessoes-portal-ms.apps.tcu.gov.br/api/sessoes
  * PDF da pauta de cada sessão (link "Baixar pauta" do portal)
  * acórdãos: a mesma Pesquisa Integrada usada por baixar_acordaos.py

Proposta de encaminhamento e número SEI não são públicos e ficam de fora.

Grava site/pauta.json. Se a lista de sessões não responder, o arquivo
anterior fica como está.

Uso:  python coletar_pauta.py [--saida site/pauta.json]
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import baixar_acordaos as ba
from coleta import salvar, sessao_http

log = logging.getLogger("pauta")
AQUI = os.path.dirname(os.path.abspath(__file__))
SESSOES_URL = "https://sessoes-portal-ms.apps.tcu.gov.br/api/sessoes"
FUSO = timezone(timedelta(hours=-3))
DIAS_FUTURO = 14   # sessões previstas até esta distância entram em "nesta semana"
DIAS_PASSADO = 7   # sessões realizadas até esta distância entram em "semana passada"

# Órgãos do MPO como aparecem na pauta (unidade jurisdicionada, interessado, consulente).
RX_MPO = re.compile(
    r"Minist[ée]rio do Planejamento e Or[çc]amento|Secretaria de Or[çc]amento Federal|"
    r"Secretaria de Coordena[çc][ãa]o e Governan[çc]a das Empresas Estatais|"
    r"Secretaria Nacional de Planejamento|Secretaria de Planejamento e Assuntos Econ[ôo]micos|"
    r"Secretaria de Monitoramento e Avalia[çc][ãa]o de Pol[íi]ticas P[úu]blicas|"
    r"Secretaria de Assuntos Internacionais e Desenvolvimento|"
    r"Instituto Brasileiro de Geografia e Estat[íi]stica|Instituto de Pesquisa Econ[ôo]mica Aplicada", re.I)
SIGLA = [(r"Planejamento e Or[çc]amento", "MPO"), (r"Or[çc]amento Federal", "SOF"),
         (r"Empresas Estatais", "SEST"), (r"Secretaria Nacional de Planejamento", "SEPLAN"),
         (r"Monitoramento e Avalia[çc][ãa]o", "SMA"), (r"Assuntos Internacionais e Desenvolvimento", "SEAID"),
         (r"Geografia e Estat[íi]stica", "IBGE"), (r"Pesquisa Econ[ôo]mica Aplicada", "IPEA")]
# Atos de pessoal (admissão, aposentadoria...) do IBGE/IPEA não interessam ao monitoramento.
RX_PESSOAL = re.compile(r"ATOS? DE (ADMISS|APOSENTADORIA|PENS|REFORMA|CONCESS)|APOSENTADORIA|PENS[ÃA]O CIVIL|ADMISS[ÃA]O", re.I)

RX_TC = re.compile(r"^(\d{3}\.\d{3}/\d{4}-\d)\s*-\s*(.*)$")
RX_MIN = re.compile(r"^(Ministr[oa](?:-Substitut[oa])?)\s+([A-ZÀ-Ý][A-ZÀ-Ý .'-]{3,})$")
RX_LIXO = re.compile(
    r"^(PAUTA d[ao] .*|Sess[ãa]o (Ordin|Extraord).*|Emitida em .*|\d{1,3}|PROCESSOS RELACIONADOS|"
    r"As transmiss[õo]es das sess[õo]es.*|dos links disponibilizados.*|https?://\S+)$", re.I)
RX_ROTULO = re.compile(r"\b(Natureza|Unidades? jurisdicionadas?|Interessad[oa]s?(?:/Respons[áa]veis)?|Respons[áa]ve(?:l|is)|"
                       r"Representa[çc][ãa]o legal|Representantes?|Consulentes?|Recorrentes?|Embargantes?|Denunciantes?)\s*:", re.I)


def texto_pdf(conteudo: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(conteudo)).pages)


def entradas(texto: str) -> list[dict]:
    """Um registro por processo da pauta: número, relator e texto completo
    (o processo pode atravessar a quebra de página)."""
    out, cur, relator = [], None, ""
    for bruta in texto.splitlines():
        l = re.sub(r"\s+", " ", bruta).strip()
        if not l or RX_LIXO.match(l):
            continue
        m = RX_MIN.match(l)
        if m:
            relator, cur = m.group(2).strip().title(), None
            continue
        m = RX_TC.match(l)
        if m:
            cur = {"tc": m.group(1), "relator": relator, "txt": m.group(2).strip()}
            out.append(cur)
            continue
        if cur:
            cur["txt"] = (cur["txt"] + " " + l).strip()
    return out


def campos(txt: str) -> dict:
    """Separa o texto do processo em assunto (o que vem antes do 1º rótulo) e rótulos."""
    partes = RX_ROTULO.split(txt)
    d = {"assunto": partes[0].strip(" .")}
    for i in range(1, len(partes) - 1, 2):
        chave = partes[i].lower()
        chave = ("natureza" if chave.startswith("natureza") else "unidades" if chave.startswith("unidade")
                 else "interessados" if chave.startswith(("interessad", "respons")) else "representacao"
                 if chave.startswith("representa") else "outros")
        d[chave] = (d.get(chave, "") + " " + partes[i + 1].strip(" .;")).strip()
    return d


def siglas(txt: str) -> list[str]:
    return [s for rx, s in SIGLA if re.search(rx, txt, re.I)]


def carregar_painel() -> tuple[dict, dict, set]:
    def ler(n):
        try:
            return json.load(open(os.path.join(AQUI, "site", n), encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    dados = {p["numero"]: p for p in ler("dados.json").get("processos", [])}
    kanban = {p["numero"]: p for p in ler("kanban.json").get("processos", [])}
    acomp = set()
    try:
        acomp = set(re.findall(r"\d{3}\.\d{3}/\d{4}-\d", open(os.path.join(AQUI, "Processos acompanhados.txt"), encoding="utf-8").read()))
    except OSError:
        pass
    return dados, kanban, acomp


def listar_sessoes(s: requests.Session) -> list[dict]:
    r = s.get(SESSOES_URL, params={"page": 0, "size": 40}, timeout=60)
    r.raise_for_status()
    return r.json().get("content") or []


def baixar_pauta(s: requests.Session, sess: dict) -> bytes | None:
    urls = [(sess.get("linkPauta") or {}).get("url"), sess.get("linkVersaoRecentePauta")]
    for u in filter(None, urls):
        try:
            r = s.get(u, timeout=90, headers={"Accept": "application/pdf,*/*"})
            if r.status_code == 200 and r.content[:5].startswith(b"%PDF"):
                return r.content
        except requests.RequestException as e:
            log.warning("pauta %s: %s", u, e)
    return None


def url_tcu(tc: str) -> str:
    digitos = re.sub(r"\D", "", tc)
    return f"https://pesquisa.apps.tcu.gov.br/#/push/processo?numero={int(digitos)}"


def resultado(s: requests.Session, tc: str, data_br: str, kp: dict | None) -> dict:
    """O que aconteceu com o processo na sessão: acórdão (Pesquisa Integrada) ou,
    para processo do painel, retirada de pauta ou pedido de vista."""
    try:
        for d in ba.buscar(s, tc):
            if str(d.get("DATASESSAO", "")).strip() == data_br:
                texto = ba.texto_limpo(d.get("ACORDAO"))
                i = texto.find("ACORDAM")
                trecho = re.sub(r"\s+", " ", texto[i if i >= 0 else 0:]).strip()
                return {"tipo": "acordao", "acordao": f"{int(d.get('NUMACORDAO'))}/{d.get('ANOACORDAO')}",
                        "colegiado": ba.texto_limpo(d.get("COLEGIADO")),
                        "dispositivo": trecho[:900] + ("…" if len(trecho) > 900 else "")}
    except Exception as e:  # a falha de um não derruba a pauta
        log.warning("acórdão de %s: %s", tc, e)
    iso = "-".join(reversed(data_br.split("/")))
    for ev in (kp or {}).get("eventos", []):
        if ev["data"] < iso:
            continue
        if re.search(r"(exclu[íi]do|retirado) d[ae] pauta", ev["desc"], re.I):
            return {"tipo": "retirado", "texto": f"Retirado de pauta ({ev['data'][8:10]}/{ev['data'][5:7]})."}
        if re.search(r"pedido de vista", ev["desc"], re.I):
            return {"tipo": "vista", "texto": "Pedido de vista na sessão: volta à pauta depois."}
        if ev.get("peso") == "decisao":
            return {"tipo": "acordao", "acordao": (ev.get("rot") or "").replace("Acórdão ", ""), "colegiado": "", "dispositivo": ""}
    return {"tipo": "pendente", "texto": "Acórdão ainda não publicado na Pesquisa Integrada."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default=os.path.join(AQUI, "site", "pauta.json"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    s = sessao_http()
    try:
        todas = listar_sessoes(s)
    except Exception as e:
        log.warning("lista de sessões indisponível (%s); %s anterior preservado", e, a.saida)
        return 0
    agora = datetime.now(FUSO)
    hoje = agora.date()
    dados, kanban, acomp = carregar_painel()
    s_ac = ba.sessao()

    sessoes, itens = [], []
    for sess in todas:
        try:
            dt = datetime.fromisoformat(sess["dataHoraPrevista"]).date()
        except (KeyError, ValueError):
            continue
        if (sess.get("situacao") or "").lower().startswith("cancel"):
            continue
        quando = "proxima" if hoje <= dt <= hoje + timedelta(days=DIAS_FUTURO) else \
                 "passada" if hoje - timedelta(days=DIAS_PASSADO) <= dt < hoje else None
        if not quando:
            continue
        reg = {"id": sess.get("id"), "quando": quando, "data": sess["dataHoraPrevista"],
               "colegiado": sess.get("colegiado"), "natureza": sess.get("naturezaSessao"),
               "situacao": sess.get("situacao"), "reservada": bool(sess.get("seReservada")),
               "pdf": (sess.get("linkPauta") or {}).get("url") or sess.get("linkVersaoRecentePauta"),
               "processos_na_pauta": None}
        sessoes.append(reg)
        if reg["reservada"]:
            continue
        pdf = baixar_pauta(s, sess)
        if not pdf:
            log.warning("sessão %s: pauta não baixada", reg["id"])
            continue
        es = entradas(texto_pdf(pdf))
        reg["processos_na_pauta"] = len(es)
        data_br = dt.strftime("%d/%m/%Y")
        for e in es:
            c = campos(e["txt"])
            alvo = " ".join([c.get("unidades", ""), c.get("interessados", ""), c.get("outros", "")])
            sg = siglas(alvo)
            if sg and set(sg) <= {"IBGE", "IPEA"} and RX_PESSOAL.search(c.get("natureza", "") + " " + c["assunto"]):
                sg = []
            no_painel, acompanhado = e["tc"] in dados, e["tc"] in acomp
            if not (sg or no_painel or acompanhado):
                continue
            motivos = ([f"na pauta: {', '.join(sg)}"] if sg else []) + \
                      (["no painel do MPO"] if no_painel else []) + (["acompanhado pela SEAI"] if acompanhado else [])
            kp = kanban.get(e["tc"])
            item = {"tc": e["tc"], "sessao": reg["id"], "quando": quando, "data": reg["data"], "colegiado": reg["colegiado"],
                    "relator": e["relator"], "assunto": c["assunto"] or (dados.get(e["tc"], {}).get("assunto") or ""),
                    "natureza": c.get("natureza", ""), "unidades": c.get("unidades", ""),
                    "siglas": sg, "motivos": motivos, "no_painel": no_painel, "url": url_tcu(e["tc"]),
                    "assunto_painel": (dados.get(e["tc"]) or kp or {}).get("assunto")}
            if quando == "passada":
                item["resultado"] = resultado(s_ac, e["tc"], data_br, kp)
                time.sleep(ba.PAUSA)
            itens.append(item)
        log.info("sessão %s %s %s: %d processos na pauta, %d do MPO", reg["id"], reg["colegiado"], data_br,
                 len(es), sum(1 for i in itens if i["sessao"] == reg["id"]))

    sessoes.sort(key=lambda x: x["data"])
    itens.sort(key=lambda x: (x["data"], x["tc"]))
    salvar({"gerado_em": agora.isoformat(timespec="seconds"), "hoje": hoje.isoformat(),
            "sessoes": sessoes, "itens": itens}, a.saida)
    log.info("%s gravado: %d sessões, %d processos do MPO", a.saida, len(sessoes), len(itens))
    return 0


if __name__ == "__main__":
    sys.exit(main())
