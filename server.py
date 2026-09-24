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


# ============================================================
# UTILITÁRIOS
# ============================================================

MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "março": 3,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

GRADE_NAMES = {
    "SD": "SD — Soldado",
    "CB": "CB — Cabo",
    "3º SGT": "3º SGT — Terceiro-Sargento",
    "3 SGT": "3º SGT — Terceiro-Sargento",
    "3ºSGT": "3º SGT — Terceiro-Sargento",
    "2º SGT": "2º SGT — Segundo-Sargento",
    "2 SGT": "2º SGT — Segundo-Sargento",
    "2ºSGT": "2º SGT — Segundo-Sargento",
    "1º SGT": "1º SGT — Primeiro-Sargento",
    "1 SGT": "1º SGT — Primeiro-Sargento",
    "1ºSGT": "1º SGT — Primeiro-Sargento",
    "ST": "ST — Subtenente",
}

FUNCTION_NAMES = {
    "CMT": "CMT — Comandante",
    "PAT": "PAT — Patrulheiro",
    "MOT": "MOT — Motociclista",
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\D", "", str(value or ""))


def esc_text(value):
    return clean(value).replace("\u200b", "")


def matricula_pattern(matricula):
    target = norm(matricula)
    if len(target) < 5:
        return ""

    if len(target) == 7:
        return (
            rf"(?<!\d){re.escape(target[:6])}"
            rf"\s*[-/.]?\s*{re.escape(target[6])}(?!\d)"
        )

    return rf"(?<!\d){re.escape(target)}(?!\d)"


def matricula_matches(text, matricula):
    pattern = matricula_pattern(matricula)
    return bool(pattern and re.search(pattern, str(text or ""), re.I))


def row_belongs_to_matricula(cells, matricula):
    """A coluna de matrícula (posição 4) é a fonte principal.

    Isso evita falsos positivos causados pelo campo 'contexto', que em
    alguns documentos contém trechos de várias linhas da tabela.
    """
    target = norm(matricula)

    if len(cells) > 4:
        cell = norm(cells[4])
        if cell:
            return cell == target

    return matricula_matches(" | ".join(cells), matricula)


# ============================================================
# DOCUMENTO / MÊS / TIPO
# ============================================================


def document_period(document):
    text = clean(document.get("titulo", ""))
    m = re.search(
        r"(janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|"
        r"setembro|outubro|novembro|dezembro)\s+de\s+(20\d{2})",
        text,
        re.I,
    )
    if m:
        month = MONTHS[m.group(1).lower()]
        year = int(m.group(2))
        return month, year

    for value in (
        document.get("documento_data", ""),
        document.get("data_protocolo", ""),
    ):
        m = re.search(r"(\d{2})/(\d{2})/(20\d{2})", str(value or ""))
        if m:
            return int(m.group(2)), int(m.group(3))

    return None, None


def full_date_from_day(day, document):
    day = clean(day)
    m = re.search(r"\b(0?[1-9]|[12]\d|3[01])\b", day)
    if not m:
        return ""

    month, year = document_period(document)
    if not month or not year:
        return ""

    try:
        d = int(m.group(1))
        datetime(year, month, d)
        return f"{d:02d}/{month:02d}/{year}"
    except ValueError:
        return ""


def classify_occurrence(document, cells):
    """Classifica cada ocorrência, não a pessoa.

    Regras gerais:
    - documento 'Escala Principal' -> Escala Principal;
    - permuta -> Permuta de Serviço;
    - 'compensação de horas' / 'concessão do CMT' -> Escala de Folga;
    - registros de serviço efetivo -> Escala de Serviço;
    - restante -> Escala de Serviço.

    Assim a mesma matrícula pode ter registros de tipos diferentes,
    sem qualquer regra específica para uma pessoa.
    """
    title = clean(document.get("titulo", ""))
    text = " | ".join(cells).upper()

    if re.search(r"ESCALA\s+PRINCIPAL", title, re.I):
        return "Escala Principal"

    if re.search(r"PERMUTA", title, re.I) or re.search(
        r"ESCALA\s+A\s+(?:ATUAL|SER\s+CUMPRIDA)", text, re.I
    ):
        return "Permuta de Serviço"

    if re.search(r"CONCESS(?:ÃO|ÂO)", text, re.I):
        return "Escala de Folga"

    if re.search(r"COMPENSAÇÃO\s+DE\s+HORAS", text, re.I):
        return "Escala de Folga"

    return "Escala de Serviço"


# ============================================================
# CAMPOS
# ============================================================


def normalize_grade(value):
    value = clean(value)
    if not value:
        return ""

    key = re.sub(r"\s+", " ", value.upper())
    return GRADE_NAMES.get(key, value)


def normalize_function(value):
    value = clean(value)
    if not value:
        return ""
    return FUNCTION_NAMES.get(value.upper(), value)


def extract_function(text):
    text = str(text or "")
    m = re.search(r"\((CMT|PAT|MOT)\)", text, re.I)
    if m:
        return m.group(1).upper()

    # Só usar CMT/PAT/MOT isolado quando houver uma indicação de que
    # ele pertence ao registro, evitando capturar palavras do contexto.
    m = re.search(r"\b(CMT|PAT|MOT)\b", text, re.I)
    return m.group(1).upper() if m else ""


def extract_grade(text):
    text = str(text or "")
    patterns = [
        r"\b(3º\s*SGT|2º\s*SGT|1º\s*SGT)\b",
        r"\b(3\s*SGT|2\s*SGT|1\s*SGT)\b",
        r"\b(3ºSGT|2ºSGT|1ºSGT)\b",
        r"\b(CB|SD|ST)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return normalize_grade(m.group(1))
    return ""


def normalize_service(value):
    value = esc_text(value)
    if not value:
        return ""

    # Retirar matrícula/graduação/função que eventualmente foram
    # incorporadas ao texto da célula por causa de células mescladas.
    value = re.sub(
        r"\b(?:3º?\s*SGT|2º?\s*SGT|1º?\s*SGT|CB|SD|ST)\s+"
        r"\d{6}[-/.]?\d\b",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\b\d{6}[-/.]?\d\b", "", value)
    value = re.sub(r"\b(?:CMT|PAT|MOT)\b", "", value, flags=re.I)
    value = re.sub(r"\([^)]*\)", "", value)

    # Remover dias/horários que podem ter sido anexados à célula.
    value = re.sub(
        r"\b(?:0?[1-9]|[12]\d|3[01])(?:\s*,\s*(?:0?[1-9]|[12]\d|3[01]))+"
        r"(?:\s*(?:e|,)\s*(?:0?[1-9]|[12]\d|3[01]))?\s*\.?",
        "",
        value,
    )
    value = re.sub(r"\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b", "", value, flags=re.I)

    value = re.sub(r"M\.?\s*O\.?\s*T[ÁA]TICO", "MO Tático", value, flags=re.I)
    value = re.sub(r"M\.?\s*O\.?\s*(\d+(?:\.\d+)?)", r"M.O. \1", value, flags=re.I)
    value = re.sub(r"\s*-\s*M\.?\s*O\.?\s*", " — M.O. ", value, flags=re.I)

    return clean(value).strip(" -—,.")


def extract_vehicle(text):
    text = str(text or "")
    found = []

    patterns = [
        (r"M\.?\s*O\.?\s*(?:T[ÁA]TICO\s*[-—]?\s*)?(\d+(?:\.\d+)?)", "M.O."),
        (r"\bGT\s*[-—]?\s*(\d+(?:\.\d+)?)", "GT"),
        (r"\bPCR\s*[-—]?\s*(\d+(?:\.\d+)?)", "PCR"),
    ]

    for pattern, prefix in patterns:
        for m in re.finditer(pattern, text, re.I):
            item = f"{prefix} {m.group(1)}"
            if item not in found:
                found.append(item)

    return ", ".join(found)


def normalize_time(value):
    value = clean(value)
    if not value:
        return ""

    value = re.sub(r"\s*(?:/|às|as)\s*", " às ", value, flags=re.I)
    value = re.sub(r"\b(\d{1,2})h(\d{1,2})\b", lambda m: f"{int(m.group(1)):02d}h{m.group(2)}", value, flags=re.I)
    value = re.sub(r"\b(\d{1,2})h\b", lambda m: f"{int(m.group(1)):02d}h", value, flags=re.I)
    return clean(value)


def extract_time(text):
    text = str(text or "")
    patterns = [
        r"\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b",
        r"\b\d{1,2}h\d{0,2}\s*(?:às|as|-)\s*\d{1,2}h\d{0,2}\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return normalize_time(m.group(0))
    return ""


def extract_day_list(text):
    text = clean(text)

    patterns = [
        r"(?<!\d)((?:0?[1-9]|[12]\d|3[01])(?:\s*,\s*(?:0?[1-9]|[12]\d|3[01]))+"
        r"(?:\s*(?:e|,)\s*(?:0?[1-9]|[12]\d|3[01]))?)(?:\s*\.)?",
        r"(?<!\d)((?:0?[1-9]|[12]\d|3[01])(?:\s+|\s*,\s*)(?:0?[1-9]|[12]\d|3[01])"
        r"(?:\s*(?:,|e)\s*(?:0?[1-9]|[12]\d|3[01]))+)(?:\s*\.)?",
    ]

    found = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, re.I))

    if not found:
        return ""

    # Preferir o maior agrupamento de dias, normalmente o da escala.
    best = max(found, key=lambda x: len(re.findall(r"\b\d{1,2}\b", x)))
    return clean(best).replace(" ,", ",")


def extract_name(cells, matricula):
    if len(cells) > 5 and norm(cells[4]) == norm(matricula):
        name = esc_text(cells[5])
        if name:
            # Remover função e anotações de dias/horários que grudaram no nome.
            name = re.sub(r"\s*\((?:CMT|PAT|MOT)\).*", "", name, flags=re.I)
            name = re.sub(r"\s+(?:\d{1,2}(?:\s*,\s*\d{1,2})+|e\s+\d{1,2}).*$", "", name, flags=re.I)
            name = re.sub(r"\s+(?:concessão|concessâo|compensação).*$", "", name, flags=re.I)
            name = re.sub(r"\s*\*a/c.*$", "", name, flags=re.I)
            return clean(name)

    text = " | ".join(cells)
    p = matricula_pattern(matricula)
    if p:
        m = re.search(
            rf"{p}\s+([A-ZÀ-Ú][A-ZÀ-Ú .'-]{{2,}}?)(?:\s*\((?:CMT|PAT|MOT)\)|\s+(?:CMT|PAT|MOT)\b)",
            text,
            re.I,
        )
        if m:
            return clean(m.group(1))

    return ""


def extract_team(text):
    for team in ("ALFA", "BRAVO", "CHARLIE", "DELTA"):
        if re.search(rf"\b{team}\b", str(text or ""), re.I):
            return team
    m = re.search(r"\bequipe\s+([A-ZÀ-Ú0-9_-]+)", str(text or ""), re.I)
    return clean(m.group(1)).upper() if m else ""


def extract_observation(text):
    text = clean(text)
    found = []
    patterns = [
        r"CONCESS(?:ÃO|ÂO)\s+DO\s+CMT",
        r"COMPENSAÇÃO\s+DE\s+HORAS",
        r"MUDANÇA\s+DE\s+OME",
        r"REMANEJADO(?:\s+PARA\s+O\s+DIA)?[^|;]*",
        r"VIAGEM",
        r"A/C[^|;]*",
        r"ESCALA\s+ESPECÍFICA",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            item = clean(m.group(0))
            if item and item.upper() not in {x.upper() for x in found}:
                found.append(item)
    return "; ".join(found)


def row_date(cells, document):
    # Permutas normalmente têm a data completa na coluna de dia.
    if len(cells) > 6:
        value = clean(cells[6])
        m = re.search(r"\b\d{1,2}/\d{1,2}/20\d{2}\b", value)
        if m:
            return m.group(0)

        # Escalas mensais: a coluna contém o dia do serviço.
        if re.fullmatch(r"\d{1,2}", value):
            return full_date_from_day(value, document)

    text = " | ".join(cells)
    m = re.search(r"\b\d{1,2}/\d{1,2}/20\d{2}\b", text)
    if m:
        return m.group(0)

    return ""


def row_days(cells, document, tipo):
    if tipo != "Escala Principal":
        return ""

    # A extração do PDF perdeu algumas células mescladas. Nesses casos,
    # os dias continuam presentes no texto da célula de serviço/nome.
    for value in cells:
        days = extract_day_list(value)
        if days:
            return days

    return ""


# ============================================================
# INFERÊNCIA DE UMA LINHA
# ============================================================


def infer_row(row, matricula, document):
    cells = [esc_text(x) for x in (row or [])]
    if not row_belongs_to_matricula(cells, matricula):
        return None

    text = " | ".join(x for x in cells if x)
    tipo = classify_occurrence(document, cells)

    grade = normalize_grade(cells[3]) if len(cells) > 3 else ""
    name = extract_name(cells, matricula)
    function = extract_function(cells[8] if len(cells) > 8 and cells[8] else (cells[5] if len(cells) > 5 else ""))
    service = normalize_service(cells[0] if cells else "")
    vehicle = extract_vehicle(text)
    team = extract_team(text)
    time = normalize_time(cells[7]) if len(cells) > 7 else ""
    date = row_date(cells, document)
    days = row_days(cells, document, tipo)
    observation = extract_observation(text)

    if not grade:
        grade = extract_grade(cells[5] if len(cells) > 5 else text)
    if not name:
        name = extract_name(cells, matricula)
    if not function:
        function = extract_function(text)
    if not time:
        time = extract_time(text)
    if not vehicle:
        vehicle = extract_vehicle(cells[0] if cells else text)
    if not team:
        team = extract_team(cells[1] if len(cells) > 1 else text)

    if not service:
        # Prefer the first cell, mas use padrões conhecidos no contexto.
        m = re.search(
            r"((?:MO\s*T[ÁA]TICO|GE\s*T[ÁA]TICO|GT|GTR|CURSO\s+APH)[^|;]*?)"
            r"(?=\s+(?:\d{1,2}h|\d{1,2}/\d{1,2}/20\d{2})|$)",
            text,
            re.I,
        )
        if m:
            service = normalize_service(m.group(1))

    if not date and tipo != "Escala Principal":
        date = document.get("documento_data") or document.get("data_protocolo") or ""

    situation = {
        "Escala Principal": "Regular",
        "Escala de Folga": "Folga",
        "Escala de Serviço": "Escala de Serviço",
        "Permuta de Serviço": "Permuta",
    }.get(tipo, tipo)

    # Se houver uma indicação explícita de situação especial, preservá-la.
    if re.search(r"ESCALA\s+ESPECÍFICA", text, re.I):
        situation = "Escala Específica"

    return {
        "data": date,
        "servico": service,
        "equipe": team,
        "graduacao": grade,
        "matricula": matricula,
        "nome": name,
        "funcao": normalize_function(function),
        "viatura": vehicle,
        "horario": time,
        "dias": days,
        "observacao": observation,
        "situacao": situation,
        "tipo_documento": tipo,
        "titulo_exibicao": tipo,
        "contexto": text,
    }


# ============================================================
# CACHE / CONSULTA
# ============================================================


def load_data():
    if not DATA_FILE.exists():
        return None
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def result_priority(result):
    priorities = {
        "Escala Principal": 0,
        "Escala de Folga": 1,
        "Escala de Serviço": 2,
        "Permuta de Serviço": 3,
    }
    return priorities.get(result.get("tipo_documento", ""), 9)


def date_sort_value(value):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(value or ""))
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return (9999, 99, 99)


def consult(matricula):
    original = clean(matricula)
    normalized = norm(original)

    if len(normalized) < 5:
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
            hit = infer_row(row, original, document)
            if hit:
                occurrences.append(hit)

        if not occurrences:
            continue

        # Ordenar ocorrências internas pela data quando existir.
        occurrences.sort(key=lambda o: date_sort_value(o.get("data", "")))

        # Cada documento pode conter ocorrências de tipos diferentes.
        # Agrupamos por tipo para que a interface possa exibir cada registro
        # como uma ocorrência independente.
        groups = {}
        for occurrence in occurrences:
            groups.setdefault(occurrence["tipo_documento"], []).append(occurrence)

        for tipo, items in groups.items():
            first = items[0]
            resultados.append({
                "id": document.get("id", ""),
                "titulo": document.get("titulo", "Documento SEI"),
                "titulo_exibicao": tipo,
                "data_protocolo": document.get("data_protocolo", ""),
                "documento_data": document.get("documento_data", ""),
                "url": document.get("url", ""),
                "tipo_documento": tipo,
                "ocorrencias": items,
            })

    resultados.sort(
        key=lambda r: (
            result_priority(r),
            min(
                [date_sort_value(o.get("data", "")) for o in r["ocorrencias"]],
                default=(9999, 99, 99),
            ),
            r.get("id", ""),
        )
    )

    # Perfil: a Escala Principal tem prioridade, mas não existe nenhuma
    # informação específica de matrícula embutida aqui.
    profile = {
        "nome": "",
        "graduacao": "",
        "funcao": "",
        "servico": "",
        "viatura": "",
        "equipe": "",
        "horario": "",
        "jornada": "24 × 72",
    }

    principal_occurrences = []
    secondary_occurrences = []
    for result in resultados:
        for occurrence in result["ocorrencias"]:
            if occurrence.get("tipo_documento") == "Escala Principal":
                principal_occurrences.append(occurrence)
            else:
                secondary_occurrences.append(occurrence)

    # Nome, graduação e função: a escala principal é a referência principal.
    for occurrence in principal_occurrences + secondary_occurrences:
        for key in ("nome", "graduacao", "funcao"):
            if not profile[key] and occurrence.get(key):
                profile[key] = occurrence[key]

    # Serviço/viatura/equipe/horário: preferir uma ocorrência real de serviço
    # ou folga, pois a célula mesclada da escala principal pode carregar o
    # texto do comandante/grupo inteiro.
    for occurrence in secondary_occurrences + principal_occurrences:
        for key in ("servico", "viatura", "equipe", "horario"):
            if not profile[key] and occurrence.get(key):
                profile[key] = occurrence[key]

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


# ============================================================
# ROTAS
# ============================================================

@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    data = load_data()
    return jsonify({
        "ok": True,
        "service": "minhas-escalas",
        "version": "8.0",
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
        })

    return jsonify({
        "ok": True,
        "base_pronta": bool(data.get("documentos")),
        "version": 8,
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

    documentos = data.get("documentos", [])
    return jsonify({
        "ok": True,
        "base_pronta": bool(documentos),
        "mensagem": "O servidor está funcionando e a base local de escalas está disponível.",
        "documentos": len(documentos),
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
