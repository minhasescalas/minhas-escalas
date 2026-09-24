from flask import Flask, jsonify, request, send_from_directory
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import re
import os
import time
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

PROCESS_URL = 'https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php?id_acesso_externo=709257&infra_hash=9d48ee84267766a3f78448f3246b30bf'
SEI_BASE = 'https://sei.pe.gov.br'
CONNECT_TIMEOUT = 7
READ_TIMEOUT = 18
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
CACHE_TTL = 600
DOC_CACHE_TTL = 1800
MAX_WORKERS = 4

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36 MinhasEscalas/4.1',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.6',
    'Connection': 'close',
}

_cache = {'at': 0, 'docs': None, 'meta': None}
_doc_cache = {}  # url -> {'at': timestamp, 'lines': [...], 'doc': {...}}
_lock = threading.Lock()


def norm(value):
    return re.sub(r'\D', '', value or '')


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def build_session():
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        status=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(['GET']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def get(url, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers['Referer'] = referer
    session = build_session()
    try:
        r = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or r.encoding
        return r.text
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError(f'CONNECT_TIMEOUT: {url}') from exc
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(f'READ_TIMEOUT: {url}') from exc
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f'CONNECTION_ERROR: {url} ({exc})') from exc
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 'unknown'
        raise RuntimeError(f'HTTP_ERROR_{code}: {url}') from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f'REQUEST_ERROR: {url} ({exc})') from exc


def process_documents(force=False):
    now = time.time()
    with _lock:
        if not force and _cache['docs'] is not None and now - _cache['at'] < CACHE_TTL:
            return _cache['docs'], _cache['meta']

    raw = get(PROCESS_URL)
    soup = BeautifulSoup(raw, 'html.parser')
    docs = []
    seen = set()
    meta = {'processo': 'ESCALA PELOTÃO TÁTICO SETEMBRO 2026', 'total_registros': 0}

    text = soup.get_text(' ', strip=True)
    m = re.search(r'Lista de Protocolos \((\d+) registros\)', text, re.I)
    if m:
        meta['total_registros'] = int(m.group(1))

    for a in soup.find_all('a', href=True):
        href = a.get('href', '')
        if 'documento_consulta_externa.php' not in href:
            continue
        url = urljoin(PROCESS_URL, href)
        if url in seen:
            continue
        seen.add(url)
        label = clean(a.get_text(' ', strip=True))
        row = a.find_parent('tr')
        cells = [clean(c.get_text(' ', strip=True)) for c in row.find_all(['td', 'th'])] if row else []
        doc_number = label if re.fullmatch(r'\d+', label or '') else ''
        doc_type = cells[2] if len(cells) >= 3 else ''
        doc_date = cells[3] if len(cells) >= 4 else ''
        docs.append({
            'id': doc_number,
            'titulo': doc_type or f'Documento {doc_number or "SEI"}',
            'data_protocolo': doc_date,
            'url': url,
        })

    with _lock:
        _cache.update({'at': now, 'docs': docs, 'meta': meta})
    return docs, meta


def lines_from(raw):
    soup = BeautifulSoup(raw, 'html.parser')
    for x in soup(['script', 'style', 'noscript']):
        x.decompose()
    rows = []
    for tr in soup.find_all('tr'):
        t = clean(tr.get_text(' | ', strip=True))
        if t:
            rows.append(t)
    if rows:
        return rows
    return [clean(x.get_text(' ', strip=True)) for x in soup.find_all(['p', 'div', 'td']) if clean(x.get_text(' ', strip=True))]


def matricula_matches(line, matricula):
    target = norm(matricula)
    if not target:
        return False
    # Accept common SEI formats such as 113260-1, 113260 1 or 1132601,
    # while avoiding accidental substring matches inside larger numbers.
    compact = norm(line)
    if target not in compact:
        return False
    variants = [
        re.escape(matricula.strip()),
        re.escape(re.sub(r'[^0-9]', '', matricula)),
    ]
    if '-' in matricula:
        base, check = matricula.split('-', 1)
        variants.extend([rf'{re.escape(base)}\s*-\s*{re.escape(check)}', rf'{re.escape(base)}\s+{re.escape(check)}'])
    return any(re.search(v, line, re.I) for v in variants)


def find_hits(lines, matricula):
    hits = []
    for i, line in enumerate(lines):
        if matricula_matches(line, matricula):
            context = ' '.join(lines[max(0, i - 2):min(len(lines), i + 3)])
            hits.append({'linha': line, 'contexto': context})
    return hits


def infer_from_context(lines, hit, matricula):
    ctx = hit['contexto']
    out = {
        'nome': '', 'graduacao': '', 'funcao': '', 'servico': '',
        'viatura': '', 'equipe': '', 'horario': '', 'dias': '',
        'observacao': '', 'data': ''
    }
    parts = [clean(x) for x in ctx.split('|') if clean(x)]
    if len(parts) >= 7:
        out['servico'] = parts[0]
        out['equipe'] = parts[1]
        out['graduacao'] = parts[3]
        out['dias'] = parts[-2]
        out['horario'] = parts[-1]
        out['nome'] = parts[5]
        vm = re.findall(r'(?:M\.?O\.?|GT|PCR)\s*\d+(?:\.\d+)?', parts[0], re.I)
        if vm:
            out['viatura'] = ', '.join(dict.fromkeys(vm))
        for p in parts:
            if matricula_matches(p, matricula):
                idx = parts.index(p)
                if idx + 1 < len(parts):
                    out['nome'] = parts[idx + 1]
                break
    if not out['horario']:
        m = re.search(r'\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b', ctx, re.I)
        if m:
            out['horario'] = m.group(0)
    if not out['dias']:
        m = re.search(r'\b(?:\d{1,2}[, ]*){2,}(?:e\s*)?\d{1,2}\b', ctx)
        if m:
            out['dias'] = m.group(0)
    m = re.search(r'\b\d{2}/\d{2}/\d{4}\b', ctx)
    if m:
        out['data'] = m.group(0)
    fm = re.search(r'([A-ZÀ-Ú][A-ZÀ-Ú .-]{2,})\s*\((CMT|PAT|MOT)\)', ctx, re.I)
    if fm:
        out['funcao'] = fm.group(2).upper()
        if not out['nome']:
            out['nome'] = fm.group(1).strip()
    if '*' in ctx:
        m = re.search(r'\*[^|]{0,120}', ctx)
        if m:
            out['observacao'] = clean(m.group(0))
    return out


def load_document(doc, force=False):
    now = time.time()
    with _lock:
        cached = _doc_cache.get(doc['url'])
        if cached and not force and now - cached['at'] < DOC_CACHE_TTL:
            return cached['lines']
    raw = get(doc['url'], referer=PROCESS_URL)
    lines = lines_from(raw)
    with _lock:
        _doc_cache[doc['url']] = {'at': now, 'lines': lines, 'doc': doc}
    return lines


def parse_document(doc, matricula, force=False):
    try:
        lines = load_document(doc, force=force)
        hits = find_hits(lines, matricula)
        if not hits:
            return None
        details = []
        for h in hits:
            d = infer_from_context(lines, h, matricula)
            details.append({**h, **d})
        head = ' '.join(lines[:40])
        title = doc['titulo']
        m = re.search(r'ESCALA[^|]{0,100}', head, re.I)
        if m and len(m.group(0)) < 140:
            title = clean(m.group(0))
        dm = re.search(r'\b\d{2}/\d{2}/\d{4}\b', head)
        return {
            **doc,
            'titulo': title,
            'ocorrencias': details,
            'documento_data': dm.group(0) if dm else doc.get('data_protocolo', '')
        }
    except Exception as exc:
        # Keep the document-level failure from aborting the entire search.
        return {'erro': str(exc), **doc, 'ocorrencias': []}


def build_profile(results, matricula):
    profile = {
        'nome': '', 'graduacao': '', 'funcao': '', 'servico': '',
        'viatura': '', 'equipe': '', 'horario': '', 'jornada': '24 x 72'
    }
    for r in results:
        for o in r.get('ocorrencias', []):
            for k in ['nome', 'graduacao', 'funcao', 'servico', 'viatura', 'equipe', 'horario']:
                if not profile[k] and o.get(k):
                    profile[k] = o[k]
    return profile


def consult(matricula, force=False):
    original = matricula
    n = norm(matricula)
    if len(n) < 5:
        return {'ok': False, 'erro': 'Digite uma matrícula válida (ex.: 113260-1).'}

    try:
        docs, meta = process_documents(force=force)
    except Exception as exc:
        print(f'[SEI] falha ao acessar processo: {exc}', flush=True)
        return {
            'ok': False,
            'codigo': 'SEI_INDISPONIVEL',
            'erro': 'O servidor não conseguiu acessar o SEI de Pernambuco neste momento.',
            'detalhe_tecnico': str(exc),
            'matricula': original,
            'fonte': PROCESS_URL,
        }

    results = []
    failed = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {pool.submit(parse_document, d, n, force): d for d in docs}
        for f in as_completed(future_map):
            item = f.result()
            if item and item.get('ocorrencias'):
                results.append(item)
            elif item and item.get('erro'):
                failed.append({'id': item.get('id'), 'erro': item.get('erro')})

    results.sort(key=lambda x: (x.get('documento_data') or '99/99/9999', x.get('id') or ''))
    profile = build_profile(results, n)
    return {
        'ok': True,
        'matricula': original,
        'matricula_normalizada': n,
        'total_registros_processo': meta.get('total_registros', len(docs)),
        'documentos_acessiveis': len(docs),
        'documentos_com_ocorrencia': len(results),
        'documentos_com_falha': len(failed),
        'perfil': profile,
        'resultados': results,
        'fonte': PROCESS_URL,
        'atualizado_em': time.strftime('%d/%m/%Y %H:%M:%S'),
    }


@app.get('/')
def home():
    return send_from_directory(BASE_DIR, 'index.html')


@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'minhas-escalas', 'version': '4.1'})


@app.get('/api/diagnostico')
def diagnostico():
    started = time.time()
    result = {'ok': False, 'version': '4.1', 'tempo_ms': 0}
    try:
        raw = get(SEI_BASE)
        result.update({'ok': True, 'sei': 'acessivel', 'bytes': len(raw)})
    except Exception as exc:
        result.update({'sei': 'indisponivel', 'erro': str(exc)})
    result['tempo_ms'] = round((time.time() - started) * 1000)
    return jsonify(result)


@app.get('/api/consultar')
def api_consultar():
    try:
        return jsonify(consult(request.args.get('matricula', '')))
    except Exception as exc:
        print(f'[API] erro inesperado: {type(exc).__name__}: {exc}', flush=True)
        return jsonify({
            'ok': False,
            'codigo': 'ERRO_INTERNO',
            'erro': 'Não foi possível concluir a consulta agora.',
        }), 502


@app.get('/api/atualizar')
def api_atualizar():
    try:
        docs, meta = process_documents(force=True)
        # Refresh the local document cache in one explicit operation.
        ok = 0
        falhas = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(load_document, d, True) for d in docs]
            for f in as_completed(futures):
                try:
                    f.result()
                    ok += 1
                except Exception as exc:
                    falhas += 1
                    print(f'[ATUALIZAR] falha: {exc}', flush=True)
        return jsonify({
            'ok': True,
            'documentos_acessiveis': len(docs),
            'documentos_carregados': ok,
            'documentos_com_falha': falhas,
            'total_registros_processo': meta.get('total_registros'),
        })
    except Exception as exc:
        print(f'[ATUALIZAR] erro: {exc}', flush=True)
        return jsonify({'ok': False, 'erro': str(exc)}), 502


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
