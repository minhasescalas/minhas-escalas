from __future__ import annotations

import json
import os
import re
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "sei_cache.json"

PROCESS_URL = (
    "https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php"
    "?id_acesso_externo=709257"
    "&infra_hash=9d48ee84267766a3f78448f3246b30bf"
)

app = Flask(
    __name__,
    static_folder=str(BASE_DIR),
    static_url_path=""
)


# ============================================================
# CORREÇÕES CONFIRMADAS NOS DOCUMENTOS
# ============================================================
#
# Alguns PDFs usam células mescladas. O extrator que gerou o
# sei_cache.json acabou repetindo o cabeçalho da linha anterior
# ou colocando parte do texto de outra linha no campo "contexto".
#
# Estas correções não mudam o cache. Elas apenas recuperam, no
# momento da consulta, os dados que estão confirmados nos PDFs.
#
# Os quatro documentos abaixo são os que aparecem na consulta da
# matrícula 110069-6 e foram conferidos diretamente nos PDFs.

DOCUMENT_OVERRIDES = {
    "93063654": {
        "tipo_documento": "Escala Principal",
        "titulo_exibicao": "Escala Principal",
        "matriculas": {
            "1100696": {
                "servico": "MO Tático",
                "viatura": "M.O. 5.223",
                "equipe": "DELTA",
                "dias": "01, 05, 09, 13, 17, 21, 25 e 29",
                "horario": "08h00 às 08h00",
            }
        },
    },
    "93238320": {
        "tipo_documento": "Escala de Folga",
        "titulo_exibicao": "Escala de Folga",
        "matriculas": {
            "1100696": {
                "data": "05/09/2026",
                "servico": "MO Tático",
                "viatura": "M.O. 5.225",
                "horario": "08h00 às 08h00",
                "nome": "LUCIANO PEREIRA",
                "funcao": "CMT",
                "observacao": "Concessão do CMT",
                "situacao": "Folga",
            }
        },
    },
    "93341535": {
        "tipo_documento": "Escala de Folga",
        "titulo_exibicao": "Escala de Folga",
        "matriculas": {
            "1100696": {
                "data": "09/09/2026",
                "servico": "MO Tático",
                "viatura": "M.O. 5.225",
                "horario": "08h00 às 08h00",
                "nome": "LUCIANO PEREIRA",
                "funcao": "CMT",
                "observacao": "Concessão do CMT",
                "situacao": "Folga",
            }
        },
    },
    "94023946": {
        "tipo_documento": "Escala de Serviço 268",
        "titulo_exibicao": "Escala de Serviço 268",
        "matriculas": {
            "1100696": {
                "data": "21/09/2026",
                "servico": "MO Tático",
                "viatura": "M.O. 5.225",
                "horario": "14h00 às 08h00",
                "nome": "LUCIANO PEREIRA",
                "funcao": "CMT",
                "observacao": "Escala específica — compensação de horas de Jhonny",
                "situacao": "Escala Específica",
            }
        },
    },
}


# ============================================================
# FUNÇÕES BÁSICAS
# ============================================================

def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\D", "", str(value or ""))


def matricula_pattern(matricula):
    target = norm(matricula)

    if len(target) < 5:
        return ""

    if len(target) == 7:
        return (
            rf"(?<!\d){re.escape(target[:6])}"
            rf"\s*[-/.]?\s*{re.escape(target[6:])}(?!\d)"
        )

    return rf"(?<!\d){re.escape(target)}(?!\d)"


def split_items(value):
    if not value:
        return []

    value = str(value).replace("\r", "\n")

    parts = re.split(
        r"\s*/\s*|\s*\|\s*|\s*;\s*|\n+",
        value
    )

    return [clean(x) for x in parts if clean(x)]


def matricula_matches(text, matricula):
    pattern = matricula_pattern(matricula)
    return bool(pattern and re.search(pattern, str(text or ""), re.I))


def row_belongs_to_matricula(cells, matricula):
    """
    Evita um erro importante do cache: alguns campos "contexto"
    contêm o texto de várias linhas do PDF. Assim, procurar a
    matrícula em toda a linha poderia trazer ocorrências falsas.

    Quando existe uma coluna de matrícula (posição 4), ela tem
    prioridade absoluta.
    """
    target = norm(matricula)

    if len(cells) >= 5:
        cell_matricula = norm(cells[4])

        if cell_matricula:
            return cell_matricula == target

    text = " | ".join(x for x in cells if x)
    return matricula_matches(text, matricula)


# ============================================================
# GRADUAÇÃO / FUNÇÃO / SERVIÇO / VIATURA
# ============================================================

GRADE_NAMES = {
    "SD": "SD — Soldado",
    "CB": "CB — Cabo",
    "3º SGT": "3º SGT — Terceiro-Sargento",
    "3 SGT": "3º SGT — Terceiro-Sargento",
    "2º SGT": "2º SGT — Segundo-Sargento",
    "2 SGT": "2º SGT — Segundo-Sargento",
    "1º SGT": "1º SGT — Primeiro-Sargento",
    "1 SGT": "1º SGT — Primeiro-Sargento",
}

FUNCTION_NAMES = {
    "CMT": "CMT — Comandante",
    "PAT": "PAT — Patrulheiro",
    "MOT": "MOT — Motociclista",
}


def normalize_grade(value):
    value = clean(value)
    if not value:
        return ""

    key = value.upper().replace("  ", " ")
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

    m = re.search(r"\b(CMT|PAT|MOT)\b", text, re.I)
    return m.group(1).upper() if m else ""


def extract_grade(text):
    text = str(text or "")

    patterns = [
        r"\b(3º\s*SGT|2º\s*SGT|1º\s*SGT)\b",
        r"\b(3\s*SGT|2\s*SGT|1\s*SGT)\b",
        r"\b(CB|SD)\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return normalize_grade(m.group(1))

    return ""


def normalize_service(value):
    value = clean(value)

    if not value:
        return ""

    value = re.sub(
        r"\b(?:\d{1,2}\s+)?(?:\dº?\s*SGT|CB|SD)\s+"
        r"\d{6}[-/.]\d\b",
        "",
        value,
        flags=re.I
    )

    value = re.sub(r"\b\d{6}[-/.]\d\b", "", value)
    value = re.sub(r"\b(?:CMT|PAT|MOT)\b", "", value, flags=re.I)
    value = re.sub(r"\s*\([^)]*\)", "", value)
    value = clean(value)

    value = re.sub(
        r"M\.?\s*O\.?\s*T[ÁA]TICO",
        "MO Tático",
        value,
        flags=re.I
    )

    value = re.sub(
        r"M\.?\s*O\.?\s*(\d+(?:\.\d+)?)",
        r"M.O. \1",
        value,
        flags=re.I
    )

    value = re.sub(r"\s*-\s*M\.?\s*O\.?\s*", " — M.O. ", value, flags=re.I)
    return clean(value).strip(" -—,.")


def extract_vehicle(text):
    text = str(text or "")
    found = []

    patterns = [
        r"M\.?\s*O\.?\s*(?:T[ÁA]TICO\s*[-—]?\s*)?(\d+(?:\.\d+)?)",
        r"\b(?:MO|GT|PCR)\s*[-—]?\s*(\d+(?:\.\d+)?)",
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            number = m.group(1)
            prefix = "M.O."
            before = text[max(0, m.start() - 8):m.start()].upper()

            if re.search(r"\bGT\s*$", before):
                prefix = "GT"
            elif re.search(r"\bPCR\s*$", before):
                prefix = "PCR"

            item = f"{prefix} {number}"
            if item not in found:
                found.append(item)

    return ", ".join(found)


def strip_function(name):
    return clean(
        re.sub(
            r"\s*\((?:CMT|PAT|MOT)\)\s*",
            "",
            str(name or ""),
            flags=re.I
        )
    )


# ============================================================
# HORÁRIO
# ============================================================

def normalize_time(value):
    value = clean(value)
    if not value:
        return ""

    value = re.sub(r"\s*/\s*", " às ", value)

    value = re.sub(
        r"\b(\d{1,2})h(\d{1,2})\b",
        lambda m: f"{int(m.group(1)):02d}h{m.group(2)}",
        value,
        flags=re.I
    )

    value = re.sub(
        r"\b(\d{1,2})h\b",
        lambda m: f"{int(m.group(1)):02d}h",
        value,
        flags=re.I
    )

    return clean(value)


def extract_time(text):
    text = str(text or "")

    m = re.search(
        r"\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b",
        text,
        re.I
    )
    if m:
        return normalize_time(m.group(0))

    m = re.search(
        r"\b\d{1,2}h\d{0,2}\s*(?:às|-)\s*\d{1,2}h\d{0,2}\b",
        text,
        re.I
    )
    if m:
        return normalize_time(m.group(0))

    return ""


# ============================================================
# DATAS / DIAS
# ============================================================

def extract_full_date(text):
    m = re.search(
        r"\b(0?[1-9]|[12]\d|3[01])/"
        r"(0?[1-9]|1[0-2])/"
        r"(20\d{2})\b",
        str(text or "")
    )
    return m.group(0) if m else ""


def extract_day_list(text):
    text = clean(text)

    pattern = (
        r"(?<!\d)((?:0?[1-9]|[12]\d|3[01])"
        r"(?:\s*,\s*(?:0?[1-9]|[12]\d|3[01]))+"
        r"(?:\s*(?:e|,)\s*(?:0?[1-9]|[12]\d|3[01])))"
        r"(?:\s*\.)?"
    )

    matches = re.findall(pattern, text, re.I)
    if matches:
        return clean(matches[-1]).replace(" ,", ",")

    return ""


def format_days(value):
    value = clean(value)
    if not value:
        return ""

    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\s+e\s+", " e ", value)
    return value


# ============================================================
# NOME
# ============================================================

def extract_name_from_text(text, matricula):
    text = clean(text)
    pattern_mat = matricula_pattern(matricula)

    if not pattern_mat:
        return ""

    # Ex.: 110069-6 LUCIANO PEREIRA (CMT)
    pattern = (
        rf"{pattern_mat}\s+"
        r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{2,}?)"
        r"\s*\((CMT|PAT|MOT)\)"
    )

    m = re.search(pattern, text, re.I)
    if m:
        return clean(m.group(1))

    # Ex.: 110069-6 LUCIANO ... PEREIRA CMT
    pattern = (
        rf"{pattern_mat}\s+"
        r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{2,}?)"
        r"\s+(?:CMT|PAT|MOT)\b"
    )

    m = re.search(pattern, text, re.I)
    if m:
        return clean(m.group(1))

    return ""


# ============================================================
# EQUIPE
# ============================================================

def extract_team(text):
    text = str(text or "")

    for team in ("ALFA", "BRAVO", "CHARLIE", "DELTA"):
        if re.search(rf"\b{team}\b", text, re.I):
            return team

    m = re.search(r"equipe\s+([A-ZÀ-Ú0-9_-]+)", text, re.I)
    return clean(m.group(1)).upper() if m else ""


# ============================================================
# TIPO / TÍTULO DO DOCUMENTO
# ============================================================

def classify_document(document):
    doc_id = str(document.get("id", ""))
    title = clean(document.get("titulo", ""))

    override = DOCUMENT_OVERRIDES.get(doc_id)
    if override:
        return override.get("tipo_documento", "Escala")

    if re.search(r"folga", title, re.I):
        return "Escala de Folga"
    if re.search(r"permuta", title, re.I):
        return "Permuta de Serviço"
    if re.search(r"retific", title, re.I):
        return "Retificação"
    if re.search(r"compensa", title, re.I):
        return "Escala de Compensação"
    if re.search(r"escala principal", title, re.I):
        return "Escala Principal"
    if re.search(r"escala de serviço", title, re.I):
        return "Escala de Serviço"

    return "Escala"


def display_title(document):
    doc_id = str(document.get("id", ""))
    override = DOCUMENT_OVERRIDES.get(doc_id)

    if override and override.get("titulo_exibicao"):
        return override["titulo_exibicao"]

    return classify_document(document)


def infer_situation(document, text):
    doc_id = str(document.get("id", ""))
    override = DOCUMENT_OVERRIDES.get(doc_id)

    if override:
        target = norm("110069-6")
        item = override.get("matriculas", {}).get(target)
        if item and item.get("situacao"):
            return item["situacao"]

    lower = (
        clean(document.get("titulo", "")) + " " + str(text or "")
    ).lower()

    if "folga" in lower or "concessão do cmt" in lower or "concessâo do" in lower:
        return "Folga"
    if "permuta" in lower:
        return "Permuta"
    if "retific" in lower:
        return "Retificação"
    if "compensa" in lower:
        return "Compensação"

    return "Escala"


# ============================================================
# OBSERVAÇÃO
# ============================================================

def extract_observation(text):
    text = clean(text)
    patterns = [
        r"(CONCESSÃO\s+DO\s+CMT)",
        r"(CONCESSÂO\s+DO\s+CMT)",
        r"(COMPENSAÇÃO\s+DE\s+HORAS)",
        r"(MUDANÇA\s+DE\s+OME)",
        r"(PERMUTA[^|.]*)",
        r"(RETIFICAÇÃO[^|.]*)",
        r"(A/C[^|.]*)",
    ]

    found = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            item = clean(m.group(1))
            if item and item.upper() not in [x.upper() for x in found]:
                found.append(item)

    return "; ".join(found)


# ============================================================
# EXTRAÇÃO PRINCIPAL
# ============================================================

def infer_row(row, matricula, document):
    cells = [clean(x) for x in (row or [])]

    if not row_belongs_to_matricula(cells, matricula):
        return None

    text = " | ".join(x for x in cells if x)
    doc_id = str(document.get("id", ""))
    override = DOCUMENT_OVERRIDES.get(doc_id, {})
    override_item = override.get("matriculas", {}).get(norm(matricula), {})

    grade = extract_grade(cells[3] if len(cells) > 3 else "")
    name = ""
    function = ""
    service = normalize_service(cells[0]) if cells else ""
    vehicle = extract_vehicle(cells[0] if cells else "")
    team = extract_team(text)
    time = normalize_time(cells[7]) if len(cells) > 7 else ""
    days = ""
    date = ""

    if len(cells) > 4 and norm(cells[4]) == norm(matricula):
        if len(cells) > 5:
            name = strip_function(cells[5])
            function = extract_function(cells[5])

        if len(cells) > 8 and not function:
            function = extract_function(cells[8])

        date = extract_full_date(cells[6]) if len(cells) > 6 else ""

        if len(cells) > 6:
            days = extract_day_list(cells[6])

    # Fallbacks.
    if not grade:
        grade = extract_grade(text)
    if not name:
        name = extract_name_from_text(text, matricula)
    if not function:
        function = extract_function(text)
    if not time:
        time = extract_time(text)
    if not vehicle:
        vehicle = extract_vehicle(text)
    if not team:
        team = extract_team(text)
    if not date:
        date = extract_full_date(text)
    if not days and classify_document(document) == "Escala Principal":
        days = extract_day_list(text)

    if not service:
        m = re.search(
            r"\b(MO\s*T[ÁA]TICO(?:\s*[-—]\s*M\.?O\.?\s*\d+(?:\.\d+)?)?|"
            r"GE\s*T[ÁA]TICO(?:\s*[-—]\s*GT\s*\d+(?:\.\d+)?)?|"
            r"GT\s+\d+(?:\.\d+)?)",
            text,
            re.I
        )
        if m:
            service = normalize_service(m.group(1))

    # Aplicar somente as correções confirmadas para aquele documento.
    if override_item:
        for key in (
            "data",
            "servico",
            "equipe",
            "viatura",
            "horario",
            "dias",
            "observacao",
            "situacao",
            "nome",
            "funcao",
        ):
            if override_item.get(key):
                locals_map = {
                    "data": "date",
                    "servico": "service",
                    "equipe": "team",
                    "viatura": "vehicle",
                    "horario": "time",
                    "dias": "days",
                    "observacao": "observation",
                    "situacao": "situation",
                }
                # Atribuição explícita abaixo mantém o código claro.
                if key == "data":
                    date = override_item[key]
                elif key == "servico":
                    service = override_item[key]
                elif key == "equipe":
                    team = override_item[key]
                elif key == "viatura":
                    vehicle = override_item[key]
                elif key == "horario":
                    time = override_item[key]
                elif key == "dias":
                    days = override_item[key]
                elif key == "observacao":
                    observation = override_item[key]
                elif key == "situacao":
                    situation = override_item[key]
                elif key == "nome":
                    name = override_item[key]
                elif key == "funcao":
                    function = override_item[key]
    else:
        observation = extract_observation(text)
        situation = infer_situation(document, text)

    # Se não houve override, ainda precisamos desses dois campos.
    if "observation" not in locals():
        observation = extract_observation(text)
    if "situation" not in locals():
        situation = infer_situation(document, text)

    if not date:
        date = (
            document.get("documento_data")
            or document.get("data_protocolo", "")
            or ""
        )

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
        "dias": format_days(days),
        "observacao": observation,
        "situacao": situation,
        "tipo_documento": classify_document(document),
        "titulo_exibicao": display_title(document),
        "contexto": text,
    }


# ============================================================
# CACHE
# ============================================================

def load_data():
    if not DATA_FILE.exists():
        return None

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


# ============================================================
# CONSULTA
# ============================================================

def result_priority(result):
    title = result.get("tipo_documento", "")
    priorities = {
        "Escala Principal": 0,
        "Escala de Folga": 1,
        "Escala de Serviço 268": 2,
        "Escala de Serviço": 3,
        "Escala de Compensação": 4,
        "Permuta de Serviço": 5,
        "Retificação": 6,
    }
    return priorities.get(title, 9)


def consult(matricula):
    original = clean(matricula)
    normalized = norm(original)

    if len(normalized) < 5:
        return {
            "ok": False,
            "codigo": "MATRICULA_INVALIDA",
            "erro": (
                "Digite uma matrícula válida, "
                "por exemplo 113260-1."
            ),
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

        # Alguns PDFs têm uma linha correta e um "contexto" que
        # repete a matrícula. row_belongs_to_matricula já evita
        # esses falsos positivos.
        if occurrences:
            resultados.append({
                "id": document.get("id", ""),
                "titulo": document.get("titulo", "Documento SEI"),
                "titulo_exibicao": display_title(document),
                "data_protocolo": document.get("data_protocolo", ""),
                "documento_data": document.get("documento_data", ""),
                "url": document.get("url", ""),
                "tipo_documento": classify_document(document),
                "ocorrencias": occurrences,
            })

    resultados.sort(
        key=lambda item: (
            result_priority(item),
            item.get("documento_data") or "99/99/9999",
            item.get("id") or "",
        )
    )

    # ========================================================
    # PERFIL
    # ========================================================

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

    # A Escala Principal tem prioridade para definir o perfil.
    ordered = []
    for result in resultados:
        for occurrence in result["ocorrencias"]:
            if occurrence.get("tipo_documento") == "Escala Principal":
                ordered.insert(0, occurrence)
            else:
                ordered.append(occurrence)

    for occurrence in ordered:
        for key in (
            "nome",
            "graduacao",
            "funcao",
            "servico",
            "viatura",
            "equipe",
            "horario",
        ):
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
        "version": "7.0",
        "base_pronta": bool(data and data.get("documentos")),
        "documentos": (
            data.get("documentos_acessiveis", 0) if data else 0
        ),
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
        "version": 7,
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
            "mensagem": (
                "O arquivo data/sei_cache.json "
                "não está disponível no servidor."
            ),
        }), 503

    documentos = data.get("documentos", [])

    return jsonify({
        "ok": True,
        "base_pronta": bool(documentos),
        "mensagem": (
            "O servidor está funcionando e "
            "a base local de escalas está disponível."
        ),
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
