from flask import Flask, jsonify, request, send_from_directory
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, re, os, time, threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

PROCESS_URL = 'https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php?id_acesso_externo=709257&infra_hash=9d48ee84267766a3f78448f3246b30bf'
TIMEOUT = 30
HEADERS = {'User-Agent': 'MinhasEscalas/4.0 (consulta publica)'}
CACHE_TTL = 600
_cache = {'at': 0, 'docs': None, 'meta': None}
_lock = threading.Lock()


def norm(value):
    return re.sub(r'\D', '', value or '')


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


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

    # Read the protocol table. This is more reliable than relying on CSS classes.
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
        cells = [clean(c.get_text(' ', strip=True)) for c in row.find_all(['td','th'])] if row else []
        doc_number = label if re.fullmatch(r'\d+', label or '') else ''
        doc_type = cells[2] if len(cells) >= 3 else ''
        doc_date = cells[3] if len(cells) >= 4 else ''
        docs.append({'id': doc_number, 'titulo': doc_type or f'Documento {doc_number or "SEI"}', 'data_protocolo': doc_date, 'url': url})

    # Some rows can be missing a clickable action on the public page. We report the count,
    # but only documents actually accessible through document_consulta_externa.php are scanned.
    with _lock:
        _cache.update({'at': now, 'docs': docs, 'meta': meta})
    return docs, meta


def lines_from(raw):
    soup = BeautifulSoup(raw, 'html.parser')
    for x in soup(['script', 'style', 'noscript']):
        x.decompose()
    # Prefer table rows because SEI scale rows preserve columns in one textual record.
    rows = []
    for tr in soup.find_all('tr'):
        t = clean(tr.get_text(' | ', strip=True))
        if t:
            rows.append(t)
    if rows:
        return rows
    return [clean(x.get_text(' ', strip=True)) for x in soup.find_all(['p','div','td']) if clean(x.get_text(' ', strip=True))]


def find_hits(lines, matricula):
    n = norm(matricula)
    hits = []
    for i, line in enumerate(lines):
        # Compare digit-only tokens so 113260-1 and 1132601 both match.
        digits = norm(line)
        if n and n in digits:
            context = ' '.join(lines[max(0, i-2):min(len(lines), i+3)])
            hits.append({'linha': line, 'contexto': context})
    return hits


def infer_from_context(lines, hit):
    ctx = hit['contexto']
    out = {'nome':'', 'graduacao':'', 'funcao':'', 'servico':'', 'viatura':'', 'equipe':'', 'horario':'', 'dias':'', 'observacao':'', 'data':''}
    # Common SEI scale row format: service | equipe | ord | grad | matriculas | efetivo | dias | horario
    parts = [clean(x) for x in ctx.split('|') if clean(x)]
    if len(parts) >= 7:
        out['servico'] = parts[0]
        out['equipe'] = parts[1]
        out['graduacao'] = parts[3]
        out['dias'] = parts[-2]
        out['horario'] = parts[-1]
        out['nome'] = parts[5]
        # Vehicle is often embedded in service label.
        vm = re.findall(r'(?:M\.?O\.?|GT|PCR)\s*\d+(?:\.\d+)?', parts[0], re.I)
        if vm:
            out['viatura'] = ', '.join(dict.fromkeys(vm))
        # Find the name associated with the requested matrícula when possible.
        try:
            mi = next(j for j,p in enumerate(parts) if norm(p) and norm(p) == norm(matricula))
            if mi + 1 < len(parts):
                out['nome'] = parts[mi + 1]
        except StopIteration:
            pass
    # General fallbacks.
    if not out['horario']:
        m = re.search(r'\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b', ctx, re.I)
        if m: out['horario'] = m.group(0)
    if not out['dias']:
        m = re.search(r'\b(?:\d{1,2}[, ]*){2,}(?:e\s*)?\d{1,2}\b', ctx)
        if m: out['dias'] = m.group(0)
    # Extract an explicit date from document text when present.
    m = re.search(r'\b\d{2}/\d{2}/\d{4}\b', ctx)
    if m: out['data'] = m.group(0)
    # Function/name markers such as (CMT), (PAT), (MOT).
    fm = re.search(r'([A-ZÀ-Ú][A-ZÀ-Ú .-]{2,})\s*\((CMT|PAT|MOT)\)', ctx, re.I)
    if fm:
        out['funcao'] = fm.group(2).upper()
        if not out['nome']: out['nome'] = fm.group(1).strip()
    if '*' in ctx:
        m = re.search(r'\*[^|]{0,100}', ctx)
        if m: out['observacao'] = clean(m.group(0))
    return out


def parse_document(doc, matricula):
    try:
        raw = get(doc['url'])
        lines = lines_from(raw)
        hits = find_hits(lines, matricula)
        if not hits:
            return None
        details = []
        for h in hits:
            d = infer_from_context(lines, h)
            details.append({**h, **d})
        # Extract document title and date from the first 40 lines.
        head = ' '.join(lines[:40])
        title = doc['titulo']
        m = re.search(r'ESCALA[^|]{0,100}', head, re.I)
        if m and len(m.group(0)) < 140:
            title = clean(m.group(0))
        dm = re.search(r'\b\d{2}/\d{2}/\d{4}\b', head)
        return {**doc, 'titulo': title, 'ocorrencias': details, 'documento_data': dm.group(0) if dm else doc.get('data_protocolo','')}
    except Exception as exc:
        return {'erro': str(exc), **doc, 'ocorrencias': []}


def build_profile(results, matricula):
    profile = {'nome':'', 'graduacao':'', 'funcao':'', 'servico':'', 'viatura':'', 'equipe':'', 'horario':'', 'jornada':'24 x 72'}
    for r in results:
        for o in r.get('ocorrencias', []):
            for k in ['nome','graduacao','funcao','servico','viatura','equipe','horario']:
                if not profile[k] and o.get(k): profile[k] = o[k]
    # Better known mapping for a scale row where the requested matrícula is in a matrícula list.
    return profile


def consult(matricula):
    n = norm(matricula)
    if len(n) < 5:
        return {'ok': False, 'erro': 'Digite uma matrícula válida (ex.: 113260-1).'}
    docs, meta = process_documents()
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(parse_document, d, n) for d in docs]
        for f in as_completed(futures):
            item = f.result()
            if item and item.get('ocorrencias'):
                results.append(item)
    results.sort(key=lambda x: (x.get('documento_data') or '99/99/9999', x.get('id') or ''))
    profile = build_profile(results, n)
    return {
        'ok': True,
        'matricula': matricula,
        'matricula_normalizada': n,
        'total_registros_processo': meta.get('total_registros', len(docs)),
        'documentos_acessiveis': len(docs),
        'documentos_com_ocorrencia': len(results),
        'perfil': profile,
        'resultados': results,
        'fonte': PROCESS_URL,
        'atualizado_em': time.strftime('%d/%m/%Y %H:%M:%S')
    }


@app.get('/')
def home():
    return send_from_directory(BASE_DIR, 'index.html')

@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'minhas-escalas'})

@app.get('/api/consultar')
def api_consultar():
    try:
        return jsonify(consult(request.args.get('matricula','')))
    except Exception as exc:
        return jsonify({'ok': False, 'erro': 'Não foi possível consultar o SEI agora.', 'detalhe': str(exc)}), 502

@app.get('/api/atualizar')
def api_atualizar():
    # Manual cache refresh endpoint; can be protected later if needed.
    try:
        docs, meta = process_documents(force=True)
        return jsonify({'ok': True, 'documentos_acessiveis': len(docs), 'total_registros_processo': meta.get('total_registros')})
    except Exception as exc:
        return jsonify({'ok': False, 'erro': str(exc)}), 502

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
