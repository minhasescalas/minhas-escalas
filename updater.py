#!/usr/bin/env python3
"""Baixa o processo público do SEI/PMPE e cria um cache local para o site.

Executado pelo GitHub Actions. Se o SEI estiver indisponível, o script falha
sem substituir o cache anterior.
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
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 35
MAX_WORKERS = 4
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36 MinhasEscalas/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
    "Connection": "close",
}


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def session() -> requests.Session:
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
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch(url: str, referer: str | None = None) -> str:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    r = session().get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def parse_process(raw: str):
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
            cells = [clean(td.get_text(" / ", strip=True)) for td in tr.find_all(["td", "th"], recursive=False)]
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


def parse_document(doc):
    raw = fetch(doc["url"], referer=PROCESS_URL)
    soup = BeautifulSoup(raw, "html.parser")
    for x in soup(["script", "style", "noscript"]):
        x.decompose()
    rows = []
    for tr in soup.find_all("tr"):
        cells = [clean(td.get_text(" / ", strip=True)) for td in tr.find_all(["td", "th"], recursive=False)]
        if cells:
            rows.append(cells)
    if not rows:
        # Fallback for documents without a conventional table.
        for node in soup.find_all(["p", "div", "td"]):
            t = clean(node.get_text(" ", strip=True))
            if t:
                rows.append([t])
    all_text = " | ".join(" | ".join(r) for r in rows[:60])
    m = re.search(r"(ESCALA[^|]{0,160})", all_text, re.I)
    title = clean(m.group(1)) if m else doc["titulo"]
    date = doc.get("data_protocolo", "")
    dm = re.search(r"\b\d{2}/\d{2}/\d{4}\b", all_text)
    if dm:
        date = dm.group(0)
    return {
        **doc,
        "titulo": title[:180],
        "documento_data": date,
        "rows": rows,
    }


def main() -> int:
    print("[V5] Acessando processo SEI...", flush=True)
    raw = fetch(PROCESS_URL)
    total, docs = parse_process(raw)
    print(f"[V5] Registros informados pelo processo: {total}; documentos com link: {len(docs)}", flush=True)
    if not docs:
        raise RuntimeError("Nenhum documento acessível foi encontrado no processo SEI.")

    good = []
    failures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {pool.submit(parse_document, d): d for d in docs}
        for future in as_completed(future_map):
            d = future_map[future]
            try:
                result = future.result()
                good.append(result)
                print(f"[V5] OK {d.get('id')}", flush=True)
            except Exception as exc:
                failures.append({"id": d.get("id"), "url": d.get("url"), "erro": str(exc)})
                print(f"[V5] FALHA {d.get('id')}: {exc}", flush=True)

    # Do not replace a previously good cache with a nearly empty result.
    if len(good) < max(1, int(len(docs) * 0.60)):
        raise RuntimeError(f"Poucos documentos foram baixados ({len(good)}/{len(docs)}); cache anterior preservado.")

    good.sort(key=lambda x: (x.get("documento_data", "99/99/9999"), x.get("id", "")))
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            old = {}

    payload = {
        "version": 5,
        "fonte": PROCESS_URL,
        "processo": "ESCALA PELOTÃO TÁTICO SETEMBRO 2026",
        "total_registros_processo": total,
        "documentos_acessiveis": len(good),
        "documentos_com_falha": len(failures),
        "falhas": failures,
        "documentos": good,
    }

    # Compare source data without the timestamp so unchanged data does not create a commit.
    def comparable(obj):
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    old_cmp = dict(old) if isinstance(old, dict) else {}
    old_cmp.pop("atualizado_em", None)
    new_cmp = dict(payload)
    new_cmp.pop("atualizado_em", None)

    if comparable(old_cmp) == comparable(new_cmp):
        print("[V5] Nenhuma alteração nos dados.", flush=True)
        return 0

    payload["atualizado_em"] = time.strftime("%d/%m/%Y %H:%M:%S", time.gmtime()) + " UTC"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[V5] Cache atualizado: {OUT} ({len(good)} documentos).", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[V5] ERRO: {exc}", file=sys.stderr, flush=True)
        raise
