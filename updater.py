#!/usr/bin/env python3
"""Atualiza a base local a partir do processo público do SEI/PMPE.

O processo é público. O GitHub Actions executa este arquivo periodicamente,
baixa os documentos disponíveis e grava data/sei_cache.json.

Não usa IA: a classificação é baseada nas seções e tabelas presentes nos
próprios documentos do SEI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROCESS_URL = os.getenv(
    "SEI_PROCESS_URL",
    "https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php?id_acesso_externo=709257&infra_hash=9d48ee84267766a3f78448f3246b30bf",
)
BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / "data" / "sei_cache.json"

CONNECT_TIMEOUT = 60
READ_TIMEOUT = 60
MAX_WORKERS = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/128 Safari/537.36 MinhasEscalas/8.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
    "Connection": "close",
}


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def session():
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch(url, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    r = session().get(
        url,
        headers=headers,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def parse_process(raw):
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ", strip=True)

    m = re.search(r"Lista de Protocolos \((\d+) registros\)", text, re.I)
    total = int(m.group(1)) if m else 0

    docs = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "documento_consulta_externa.php" not in href:
            continue

        url = urljoin(PROCESS_URL, href)
        if url in seen:
            continue
        seen.add(url)

        tr = a.find_parent("tr")
        cells = []
        if tr:
            cells = [
                clean(td.get_text(" / ", strip=True))
                for td in tr.find_all(["td", "th"], recursive=False)
            ]

        label = clean(a.get_text(" ", strip=True))
        doc_id = label if re.fullmatch(r"\d+", label or "") else ""
        doc_type = cells[2] if len(cells) >= 3 else ""
        doc_date = cells[3] if len(cells) >= 4 else ""

        docs.append({
            "id": doc_id,
            "titulo": doc_type or f"Documento {doc_id or 'SEI'}",
            "data_protocolo": doc_date,
            "url": url,
        })

    return total, docs


def table_section(table):
    pattern = re.compile(r"DESCRIÇÃO\s*:\s*ESCALA\s+(FOLGA|SERVIÇO)", re.I)
    for previous in table.find_all_previous(string=pattern):
        match = pattern.search(str(previous))
        if match:
            return f"Escala de {match.group(1).title()}"
    return ""


def parse_document(doc):
    raw = fetch(doc["url"], referer=PROCESS_URL)
    soup = BeautifulSoup(raw, "html.parser")

    for x in soup(["script", "style", "noscript"]):
        x.decompose()

    full_text = clean(soup.get_text(" ", strip=True))
    principal = (
        "JORNADA DE TRABALHO" in full_text.upper()
        and "DISTRIBUIÇÃO DO EFETIVO DISPONÍVEL" in full_text.upper()
        and "SERVIÇO/FUNÇÃO" in full_text.upper()
        and "EQUIPE" in full_text.upper()
    )

    jornada = ""
    jm = re.search(
        r"JORNADA DE TRABALHO\s*:\s*([0-9]+\s*[xX×]\s*[0-9]+)",
        full_text,
        re.I,
    )
    if jm:
        jornada = re.sub(r"\s*[xX×]\s*", " x ", jm.group(1)).strip()

    rows = []

    for table in soup.find_all("table"):
        section = table_section(table)

        for tr in table.find_all("tr"):
            cells = [
                clean(td.get_text(" ", strip=True))
                for td in tr.find_all(["td", "th"], recursive=False)
            ]
            if not cells:
                continue

            # Main scale tables have 8 columns.
            if len(cells) >= 8:
                rows.append({
                    "cells": cells,
                    "section": section,
                    "equipe": cells[1],
                })
            # Daily service/folga documents normally have 4 or 5 columns.
            elif len(cells) >= 4:
                rows.append({
                    "cells": cells,
                    "section": section,
                    "equipe": "",
                })

    # Fallback for unusual markup.
    if not rows:
        for tr in soup.find_all("tr"):
            cells = [
                clean(td.get_text(" ", strip=True))
                for td in tr.find_all(["td", "th"])
            ]
            if cells:
                rows.append({
                    "cells": cells,
                    "section": "",
                    "equipe": "",
                })

    return {
        **doc,
        "principal": principal,
        "jornada": jornada,
        "documento_data": doc.get("data_protocolo", ""),
        "rows": rows,
    }


def main():
    print("[V8] Acessando processo SEI...", flush=True)

    raw = fetch(PROCESS_URL)
    total, docs = parse_process(raw)

    print(
        f"[V8] Registros no processo: {total}; documentos com link: {len(docs)}",
        flush=True,
    )

    if not docs:
        raise RuntimeError("Nenhum documento acessível foi encontrado no processo SEI.")

    good = []
    failures = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(parse_document, d): d
            for d in docs
        }

        for future in as_completed(future_map):
            d = future_map[future]
            try:
                result = future.result()
                good.append(result)
                print(f"[V8] OK {d.get('id')}", flush=True)
            except Exception as exc:
                failures.append({
                    "id": d.get("id"),
                    "url": d.get("url"),
                    "erro": str(exc),
                })
                print(f"[V8] FALHA {d.get('id')}: {exc}", flush=True)

    if len(good) < max(1, int(len(docs) * 0.60)):
        raise RuntimeError(
            f"Poucos documentos foram baixados ({len(good)}/{len(docs)}); "
            "cache anterior preservado."
        )

    good.sort(
        key=lambda x: (
            x.get("data_protocolo", "99/99/9999"),
            x.get("id", ""),
        )
    )

    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            old = {}

    jornada = next(
        (
            d.get("jornada")
            for d in good
            if d.get("jornada")
        ),
        "24 x 72",
    )

    payload = {
        "version": 8,
        "fonte": PROCESS_URL,
        "processo": "ESCALA PELOTÃO TÁTICO SETEMBRO 2026",
        "jornada": jornada,
        "total_registros_processo": total,
        "documentos_acessiveis": len(good),
        "documentos_com_falha": len(failures),
        "falhas": failures,
        "documentos": good,
    }

    def comparable(obj):
        return json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    old_cmp = dict(old) if isinstance(old, dict) else {}
    old_cmp.pop("atualizado_em", None)

    new_cmp = dict(payload)
    new_cmp.pop("atualizado_em", None)

    if comparable(old_cmp) == comparable(new_cmp):
        print("[V8] Nenhuma alteração nos dados.", flush=True)
        return 0

    payload["atualizado_em"] = (
        time.strftime("%d/%m/%Y %H:%M:%S", time.gmtime()) + " UTC"
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"[V8] Cache atualizado: {OUT} ({len(good)} documentos).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[V8] ERRO: {exc}", file=sys.stderr, flush=True)
        raise
