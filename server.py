from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "sei_cache.json"

PROCESS_URL = (
    "https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php"
    "?id_acesso_externo=709257"
    "&infra_hash=9d48ee84267766a3f78448f3246b30bf"
)

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")

GRADE_RE = r"(?:1º\s*SGT|2º\s*SGT|3º\s*SGT|ST|CB|SD)"
PERSON_RE = re.compile(
    rf"(?P<graduacao>{GRADE_RE})\s*"
    r"(?P<matricula>\d{6}-\d)\s*"
    rf"(?P<nome>.*?)(?=\s+{GRADE_RE}\s*\d{6}-\d\b|$)",
    re.I,
)

MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\D", "", str(value or ""))


def load_data():
    if not DATA_FILE.exists():
        return None
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_vehicles(value):
    found = re.findall(
        r"(?:M\.?\s*O\.?|GT|PCR)\s*\d+(?:\.\d+)?",
        value or "",
        re.I,
    )
    return [clean(x).replace("M.O", "M.O.") for x in dict.fromkeys(found)]


def service_name(value):
    s = clean(value)
    s = re.sub(
        r"\s*[-–—]?\s*(?:M\.?\s*O\.?|GT|PCR)\s*\d+(?:\.\d+)?",
        "",
        s,
        flags=re.I,
    )
    s = clean(s).strip(" -–—")
    return s.title() if s else ""


def extract_function(value):
    m = re.search(r"\((CMT|PAT|MOT)\)", value or "", re.I)
    return m.group(1).upper() if m else ""


def clean_name(value):
    value = clean(value)
    value = re.sub(r"\s*\((?:CMT|PAT|MOT)\)\s*", " ", value, flags=re.I)
    value = re.sub(
        r"\s*\*?\s*a/c\s+dia\s+\d{1,2}\s*",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\s*\((?:compensação|compensacao|remanejado)[^)]*\)\s*",
        " ",
        value,
        flags=re.I,
    )
    return clean(value)


def extract_note(value):
    value = clean(value)
    notes = []
    for pattern in (
        r"\*?\s*a/c\s+dia\s+\d{1,2}",
        r"\((?:compensação|compensacao)[^)]*\)",
        r"\((?:remanejado[^)]*)\)",
    ):
        notes.extend(re.findall(pattern, value, flags=re.I))
    return clean(" ".join(dict.fromkeys(notes)))


def parse_people(effective):
    people = []
    for match in PERSON_RE.finditer(clean(effective)):
        raw_name = clean(match.group("nome"))
        people.append({
            "graduacao": clean(match.group("graduacao")),
            "matricula": clean(match.group("matricula")),
            "nome": clean_name(raw_name),
            "funcao": extract_function(raw_name),
            "observacao": extract_note(raw_name),
        })
    return people


def month_year(data):
    text = clean(data.get("processo", ""))
    m = re.search(
        r"(janeiro|fevereiro|março|marco|abril|maio|junho|julho|"
        r"agosto|setembro|outubro|novembro|dezembro)[^0-9]*(20\d{2})",
        text,
        re.I,
    )
    if m:
        return MONTHS[m.group(1).lower()], int(m.group(2))
    return 9, 2026


def full_date(day, data):
    m = re.search(r"\b(0?[1-9]|[12]\d|3[01])\b", clean(day))
    if not m:
        return ""
    month, year = month_year(data)
    try:
        datetime(year, month, int(m.group(1)))
        return f"{int(m.group(1)):02d}/{month:02d}/{year}"
    except ValueError:
        return ""


def normalize_time(value):
    return clean(value).replace("/", " às ")


def classify_document(document, row_section=""):
    if document.get("principal"):
        return "Escala Principal"
    title = clean(document.get("titulo", ""))
    section = clean(row_section).upper()
    if "PERMUTA" in title.upper():
        return "Permuta de Serviço"
    if "FOLGA" in section:
        return "Escala de Folga"
    if "SERVIÇO" in section or "SERVICO" in section:
        return "Escala de Serviço"
    if "FOLGA" in title.upper():
        return "Escala de Folga"
    return "Escala de Serviço"


def parse_principal_row(row, matricula, data):
    cells = [clean(x) for x in row.get("cells", [])]
    if len(cells) < 8:
        return None

    mats = re.findall(r"\d{6}-\d", cells[4])
    target = norm(matricula)
    idx = next((i for i, m in enumerate(mats) if norm(m) == target), None)
    if idx is None:
        return None

    grades = re.findall(GRADE_RE, cells[3], flags=re.I)
    name_matches = re.findall(r"([^()]+?)\s*\((CMT|PAT|MOT)\)", cells[5], flags=re.I)

    raw_name = ""
    funcao = ""
    if idx < len(name_matches):
        raw_name = clean(name_matches[idx][0])
        funcao = name_matches[idx][1].upper()

    if not raw_name:
        # Fallback: use the segment around the target's ordinal position.
        raw_name = clean(cells[5])

    vehicles = extract_vehicles(cells[0])
    vehicle = vehicles[idx] if idx < len(vehicles) else ""
    grad = grades[idx] if idx < len(grades) else ""

    return {
        "data": "",
        "servico": service_name(cells[0]),
        "equipe": clean(row.get("equipe", "")),
        "graduacao": clean(grad),
        "matricula": matricula,
        "nome": clean_name(raw_name),
        "funcao": funcao,
        "viatura": vehicle,
        "horario": normalize_time(cells[7]),
        "dias": clean(cells[6]),
        "observacao": extract_note(cells[5]),
        "situacao": "Regular",
        "tipo_documento": "Escala Principal",
    }


def parse_row(document, row, matricula, data):
    cells = [clean(x) for x in row.get("cells", [])]
    if not cells:
        return []

    text = " | ".join(cells)
    if norm(matricula) not in norm(text):
        return []

    if document.get("principal"):
        hit = parse_principal_row(row, matricula, data)
        return [hit] if hit else []

    # Standard SEI tables are usually:
    # SERVIÇO | EFETIVO | DIA | HORÁRIO | SEI
    if len(cells) < 4:
        return []

    service = cells[0]
    effective = cells[1]
    day = cells[2]
    horario = cells[3]
    section = row.get("section", "")
    tipo = classify_document(document, section)
    people = parse_people(effective)

    # Some tables have a single person without a recognizable grade prefix.
    if not people and norm(matricula) in norm(effective):
        people = [{
            "graduacao": "",
            "matricula": matricula,
            "nome": "",
            "funcao": "",
            "observacao": "",
        }]

    hits = []
    for person in people:
        if norm(person["matricula"]) != norm(matricula):
            continue

        observacao = clean(
            " ".join(
                x for x in [person.get("observacao", ""), clean(cells[4]) if len(cells) >= 5 else ""]
                if x
            )
        )

        situacao = {
            "Escala de Folga": "Folga",
            "Permuta de Serviço": "Permuta",
        }.get(tipo, "Escala de Serviço")

        hits.append({
            "data": full_date(day, data),
            "servico": service_name(service),
            "equipe": "",
            "graduacao": person.get("graduacao", ""),
            "matricula": matricula,
            "nome": person.get("nome", ""),
            "funcao": person.get("funcao", ""),
            "viatura": ", ".join(extract_vehicles(service)),
            "horario": normalize_time(horario),
            "dias": clean(day),
            "observacao": observacao,
            "situacao": situacao,
            "tipo_documento": tipo,
        })

    return hits


def date_key(value):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(value or ""))
    return (
        int(m.group(3)), int(m.group(2)), int(m.group(1))
    ) if m else (9999, 99, 99)


def result_priority(tipo):
    return {
        "Escala Principal": 0,
        "Escala de Folga": 1,
        "Escala de Serviço": 2,
        "Permuta de Serviço": 3,
    }.get(tipo, 9)


def consult(matricula):
    original = clean(matricula)
    normalized = norm(original)

    if len(normalized) < 7:
        return {
            "ok": False,
            "codigo": "MATRICULA_INVALIDA",
            "erro": "Digite uma matrícula válida, por exemplo 113260-1.",
        }

    data = load_data()
    if not data or not data.get("documentos"):
        return {
            "ok": False,
            "codigo": "BASE_AINDA_NAO_ATUALIZADA",
            "erro": "A base de escalas ainda não foi atualizada.",
        }

    resultados = []
    for document in data.get("documentos", []):
        occurrences = []
        for row in document.get("rows", []):
            occurrences.extend(parse_row(document, row, original, data))

        if not occurrences:
            continue

        occurrences.sort(key=lambda o: date_key(o.get("data", "")))
        tipo = occurrences[0].get("tipo_documento", "Escala de Serviço")

        resultados.append({
            "id": document.get("id", ""),
            "titulo": document.get("titulo", "Documento SEI"),
            "tipo_documento": tipo,
            "data_protocolo": document.get("data_protocolo", ""),
            "documento_data": document.get("documento_data", ""),
            "url": document.get("url", ""),
            "ocorrencias": occurrences,
        })

    resultados.sort(
        key=lambda r: (
            result_priority(r.get("tipo_documento", "")),
            min((date_key(o.get("data", "")) for o in r["ocorrencias"]), default=(9999, 99, 99)),
            r.get("id", ""),
        )
    )

    # Profile is taken preferentially from the main scale, not from later
    # folga/service documents, so a later occurrence cannot overwrite it.
    profile = {
        "nome": "",
        "graduacao": "",
        "funcao": "",
        "servico": "",
        "viatura": "",
        "equipe": "",
        "horario": "",
        "jornada": data.get("jornada", "24 x 72"),
    }

    for result in resultados:
        if result.get("tipo_documento") != "Escala Principal":
            continue
        if result["ocorrencias"]:
            o = result["ocorrencias"][0]
            for key in profile:
                if key != "jornada" and o.get(key):
                    profile[key] = o[key]
            break

    # Fallback if the principal scale was not available.
    if not profile["nome"]:
        for result in resultados:
            for o in result["ocorrencias"]:
                for key in ("nome", "graduacao", "funcao", "servico", "viatura", "equipe", "horario"):
                    if not profile[key] and o.get(key):
                        profile[key] = o[key]

    return {
        "ok": True,
        "matricula": original,
        "matricula_normalizada": normalized,
        "total_registros_processo": data.get("total_registros_processo", 0),
        "documentos_acessiveis": data.get("documentos_acessiveis", 0),
        "documentos_com_falha": data.get("documentos_com_falha", 0),
        "documentos_com_ocorrencia": len(resultados),
        "perfil": profile,
        "resultados": resultados,
        "fonte": data.get("fonte", PROCESS_URL),
        "atualizado_em": data.get("atualizado_em", ""),
    }


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    data = load_data()
    return jsonify({
        "ok": True,
        "service": "minhas-escalas",
        "version": "8.0-deterministico",
        "base_pronta": bool(data and data.get("documentos")),
        "documentos": data.get("documentos_acessiveis", 0) if data else 0,
        "atualizado_em": data.get("atualizado_em", "") if data else "",
    })


@app.get("/api/consultar")
def api_consultar():
    return jsonify(consult(request.args.get("matricula", "")))


@app.get("/api/status")
def api_status():
    data = load_data()
    if not data:
        return jsonify({
            "ok": False,
            "base_pronta": False,
            "mensagem": "Base ainda não atualizada.",
        }), 503

    return jsonify({
        "ok": True,
        "base_pronta": bool(data.get("documentos")),
        "version": "8.0-deterministico",
        "processo": data.get("processo", ""),
        "total_registros_processo": data.get("total_registros_processo", 0),
        "documentos_acessiveis": data.get("documentos_acessiveis", 0),
        "documentos_com_falha": data.get("documentos_com_falha", 0),
        "atualizado_em": data.get("atualizado_em", ""),
    })


@app.get("/test-sei")
def test_sei():
    data = load_data()
    if not data:
        return jsonify({
            "ok": False,
            "base_pronta": False,
            "mensagem": "O arquivo data/sei_cache.json não está disponível no servidor.",
        }), 503

    return jsonify({
        "ok": True,
        "base_pronta": bool(data.get("documentos")),
        "mensagem": "O servidor está funcionando e a base local de escalas está disponível.",
        "documentos": len(data.get("documentos", [])),
        "total_registros_processo": data.get("total_registros_processo", 0),
        "atualizado_em": data.get("atualizado_em", ""),
        "fonte": data.get("fonte", PROCESS_URL),
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )
