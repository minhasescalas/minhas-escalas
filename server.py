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
# FUNÇÕES BÁSICAS
# ============================================================

def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\D", "", str(value or ""))


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
    target = norm(matricula)

    if len(target) < 5:
        return False

    text = str(text or "")

    # Aceita 110069-6, 110069 6, 110069.6 etc.
    pattern = (
        rf"(?<!\d){re.escape(target[:6])}"
        rf"\s*[-/.]?\s*{re.escape(target[6:])}(?!\d)"
    )

    return bool(re.search(pattern, text, re.I))


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

    key = value.upper()

    return FUNCTION_NAMES.get(key, value)


def extract_function(text):
    m = re.search(
        r"\((CMT|PAT|MOT)\)",
        str(text or ""),
        re.I
    )

    if m:
        return m.group(1).upper()

    # Algumas linhas do cache já possuem a função em uma coluna.
    m = re.search(
        r"\b(CMT|PAT|MOT)\b",
        str(text or ""),
        re.I
    )

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

    # Primeiro remove pedaços típicos do cabeçalho que aparecem
    # misturados na coluna de serviço.
    value = re.sub(
        r"\b(?:\d{1,2}\s+)?"
        r"(?:\dº?\s*SGT|CB|SD)\s+"
        r"\d{6}[-/.]\d\b",
        "",
        value,
        flags=re.I
    )

    value = re.sub(
        r"\b\d{6}[-/.]\d\b",
        "",
        value
    )

    value = re.sub(
        r"\b\d{1,2}\s+(?=\d{1,2}(?:,|\.| e )\d{1,2})",
        "",
        value
    )

    value = re.sub(
        r"\b(?:CMT|PAT|MOT)\b",
        "",
        value,
        flags=re.I
    )

    value = re.sub(
        r"\s*\([^)]*\)",
        "",
        value
    )

    value = clean(value)

    # Padronização dos nomes mais comuns.
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

    value = re.sub(
        r"\s*-\s*M\.?\s*O\.?\s*",
        " — M.O. ",
        value,
        flags=re.I
    )

    value = re.sub(r"\s+", " ", value).strip(" -—,.")

    # Se o texto ficou somente com o identificador da viatura,
    # o serviço ainda pode ser recuperado pelo contexto.
    return value


def extract_vehicle(text):
    text = str(text or "")

    patterns = [
        r"M\.?\s*O\.?\s*(?:T[ÁA]TICO\s*[-—]?\s*)?(\d+(?:\.\d+)?)",
        r"\b(?:MO|M\.O\.|GT|PCR)\s*[-—]?\s*(\d+(?:\.\d+)?)",
    ]

    found = []

    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            number = m.group(1)
            prefix = "M.O."

            # GT/PCR continuam com o próprio prefixo.
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

    value = re.sub(
        r"\s*/\s*",
        " às ",
        value
    )

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
    """
    Extrai somente listas que realmente parecem dias de escala.

    Exemplos aceitos:
    01, 05, 09, 13, 17, 21, 25 e 29
    02, 06, 10, 14, 18, 22, 26 e 30
    05, 09, 13, 17, 21, 25 e 29

    Não pega números isolados como 47 (ordem do policial).
    """
    text = clean(text)

    patterns = [
        r"(?<!\d)"
        r"((?:0?[1-9]|[12]\d|3[01])"
        r"(?:\s*,\s*(?:0?[1-9]|[12]\d|3[01]))+"
        r"(?:\s*(?:e|,)\s*(?:0?[1-9]|[12]\d|3[01])))"
        r"(?:\s*\.)?",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.I)

        if matches:
            value = matches[-1]
            return clean(value).replace(" ,", ",")

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

    target = re.escape(norm(matricula))

    # Caso comum:
    # 3º SGT 110069-6 LUCIANO PEREIRA (CMT)
    pattern = (
        rf"{target}\s+"
        r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{2,}?)"
        r"\s*\((CMT|PAT|MOT)\)"
    )

    m = re.search(pattern, text, re.I)

    if m:
        return clean(m.group(1))

    # Variante sem função entre parênteses.
    pattern = (
        rf"{target}\s+"
        r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{2,})"
        r"(?=\s+\d{1,2}(?:,|\.|$)|\s*$)"
    )

    m = re.search(pattern, text, re.I)

    if m:
        name = clean(m.group(1))
        name = re.sub(
            r"\s+(?:\d{1,2}(?:,|\.|$).*)$",
            "",
            name
        )
        return clean(name)

    return ""


# ============================================================
# EQUIPE
# ============================================================

def extract_team(text):
    text = str(text or "")

    # Equipes explícitas.
    for team in ("ALFA", "BRAVO", "CHARLIE", "DELTA"):
        if re.search(rf"\b{team}\b", text, re.I):
            return team

    # Algumas linhas trazem "Equipe DELTA".
    m = re.search(
        r"equipe\s+([A-ZÀ-Ú0-9_-]+)",
        text,
        re.I
    )

    if m:
        return clean(m.group(1)).upper()

    return ""


# ============================================================
# SITUAÇÃO / TIPO DO DOCUMENTO
# ============================================================

def classify_document(document):
    title = clean(document.get("titulo", ""))

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

    if re.search(r"escala.*tático|pelotão tático", title, re.I):
        return "Escala Principal"

    return "Escala"


def infer_situation(document, text):
    title = clean(document.get("titulo", ""))
    lower = (title + " " + str(text or "")).lower()

    if "folga" in lower:
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

    # Mantém informações importantes sem colocar todo o contexto
    # do documento dentro do campo.
    patterns = [
        r"(CONCESSÃO\s+DO\s+CMT)",
        r"(COMPENSAÇÃO\s+DE\s+HORAS)",
        r"(MUDANÇA\s+DE\s+OME)",
        r"(PERMUTA[^|.]*)",
        r"(RETIFICAÇÃO[^|.]*)",
        r"(\*[^|]{0,180})",
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
# EXTRAÇÃO PRINCIPAL DA LINHA
# ============================================================

def infer_row(row, matricula, document):
    cells = [clean(x) for x in (row or [])]
    text = " | ".join(x for x in cells if x)

    if not matricula_matches(text, matricula):
        return None

    title = clean(document.get("titulo", ""))
    doc_type = classify_document(document)

    # A coluna 4 normalmente contém a matrícula.
    # A coluna 5 normalmente contém o nome.
    grade = ""
    name = ""
    function = ""
    service = ""
    vehicle = ""
    team = ""
    time = ""
    days = ""
    date = ""

    if len(cells) >= 8:
        grade = extract_grade(cells[3])

        if norm(cells[4]) == norm(matricula):
            name = strip_function(cells[5])
            function = extract_function(cells[5])

        # Serviço: só usar diretamente quando a célula parece
        # realmente um serviço, evitando jogar o cabeçalho inteiro.
        raw_service = cells[0]

        if raw_service:
            service = normalize_service(raw_service)

        vehicle = extract_vehicle(raw_service)

        # Em alguns documentos a função fica na coluna 8.
        if not function and len(cells) > 8:
            function = extract_function(cells[8])

        time = normalize_time(cells[7])

        # A coluna 6 pode ser dia, lista de dias ou data.
        date = extract_full_date(cells[6])

        if not date and doc_type == "Escala Principal":
            days = extract_day_list(cells[5])

        if not days:
            days = extract_day_list(cells[6])

    # ========================================================
    # FALLBACKS PELO TEXTO COMPLETO
    # ========================================================

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

    if not days and doc_type == "Escala Principal":
        days = extract_day_list(text)

    # ========================================================
    # SERVIÇO MAIS LIMPO
    # ========================================================

    if service:
        # Se a célula veio com "MO TÁTICO - M.O 5.225 CB",
        # remove apenas o excesso de graduação.
        service = re.sub(
            r"\s+(?:3º?\s*SGT|2º?\s*SGT|1º?\s*SGT|CB|SD)\s*$",
            "",
            service,
            flags=re.I
        )
        service = clean(service)

    if not service:
        # Tenta identificar somente o nome do serviço, sem
        # capturar o cabeçalho inteiro.
        service_match = re.search(
            r"\b(MO\s*T[ÁA]TICO|GE\s*T[ÁA]TICO|GT\s+\d+(?:\.\d+)?|"
            r"PELOTÃO\s+TÁTICO\s*/\s*[A-ZÀ-Ú ]+)",
            text,
            re.I
        )

        if service_match:
            service = normalize_service(service_match.group(1))

    # Se o serviço for somente o identificador de viatura,
    # tenta recuperar o nome a partir do contexto.
    if service and re.fullmatch(r"(?:M\.O\.|GT|PCR)\s*[\d.]+", service, re.I):
        m = re.search(
            r"\b(MO\s*T[ÁA]TICO|GE\s*T[ÁA]TICO|GT\s+\d+(?:\.\d+)?)",
            text,
            re.I
        )

        if m:
            service = normalize_service(m.group(1))

    # ========================================================
    # DATA DO DOCUMENTO COMO ÚLTIMO RECURSO
    # ========================================================

    if not date:
        date = (
            document.get("documento_data")
            or document.get("data_protocolo", "")
            or ""
        )

    # ========================================================
    # OBSERVAÇÃO
    # ========================================================

    observation = extract_observation(text)

    return {
        "data": date,
        "servico": service,
        "equipe": team,
        "graduacao": grade,
        "matricula": matricula,
        "nome": name,
        "funcao": function,
        "viatura": vehicle,
        "horario": time,
        "dias": format_days(days),
        "observacao": observation,
        "situacao": infer_situation(document, text),
        "tipo_documento": doc_type,
        "contexto": text,
    }


# ============================================================
# CACHE
# ============================================================

def load_data():
    if not DATA_FILE.exists():
        return None

    try:
        return json.loads(
            DATA_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return None


# ============================================================
# CONSULTA
# ============================================================

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
            "erro": (
                "A base de escalas ainda não foi "
                "atualizada."
            ),
        }

    resultados = []

    for document in data.get("documentos", []):
        occurrences = []

        for row in document.get("rows", []):
            hit = infer_row(
                row,
                original,
                document
            )

            if hit:
                occurrences.append(hit)

        if occurrences:
            resultados.append({
                "id": document.get("id", ""),
                "titulo": document.get(
                    "titulo",
                    "Documento SEI"
                ),
                "data_protocolo": document.get(
                    "data_protocolo",
                    ""
                ),
                "documento_data": document.get(
                    "documento_data",
                    ""
                ),
                "url": document.get("url", ""),
                "tipo_documento": classify_document(document),
                "ocorrencias": occurrences,
            })

    # Mais recentes primeiro quando houver data.
    resultados.sort(
        key=lambda item: (
            item.get("documento_data") or "99/99/9999",
            item.get("id") or ""
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

    # Dar preferência à Escala Principal para montar o perfil.
    ordered_occurrences = []

    for result in resultados:
        for occurrence in result["ocorrencias"]:
            if occurrence.get("tipo_documento") == "Escala Principal":
                ordered_occurrences.insert(0, occurrence)
            else:
                ordered_occurrences.append(occurrence)

    for occurrence in ordered_occurrences:
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

        "total_registros_processo": data.get(
            "total_registros_processo",
            0
        ),

        "documentos_acessiveis": data.get(
            "documentos_acessiveis",
            0
        ),

        "documentos_com_falha": data.get(
            "documentos_com_falha",
            0
        ),

        "documentos_com_ocorrencia": len(resultados),

        "perfil": profile,
        "resultados": resultados,

        "fonte": data.get(
            "fonte",
            PROCESS_URL
        ),

        "atualizado_em": data.get(
            "atualizado_em",
            ""
        ),
    }


# ============================================================
# ROTAS
# ============================================================

@app.get("/")
def home():
    return send_from_directory(
        BASE_DIR,
        "index.html"
    )


@app.get("/health")
def health():
    data = load_data()

    return jsonify({
        "ok": True,
        "service": "minhas-escalas",
        "version": "6.0",
        "base_pronta": bool(
            data and data.get("documentos")
        ),
        "documentos": (
            data.get(
                "documentos_acessiveis",
                0
            )
            if data
            else 0
        ),
        "atualizado_em": (
            data.get(
                "atualizado_em",
                ""
            )
            if data
            else ""
        ),
    })


@app.get("/api/consultar")
def api_consultar():
    return jsonify(
        consult(
            request.args.get(
                "matricula",
                ""
            )
        )
    )


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
        "base_pronta": bool(
            data.get("documentos")
        ),
        "version": data.get("version", 6),
        "processo": data.get("processo", ""),
        "total_registros_processo": data.get(
            "total_registros_processo",
            0
        ),
        "documentos_acessiveis": data.get(
            "documentos_acessiveis",
            0
        ),
        "documentos_com_falha": data.get(
            "documentos_com_falha",
            0
        ),
        "atualizado_em": data.get(
            "atualizado_em",
            ""
        ),
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
        "total_registros_processo": data.get(
            "total_registros_processo",
            0
        ),
        "atualizado_em": data.get(
            "atualizado_em",
            ""
        ),
        "fonte": data.get(
            "fonte",
            PROCESS_URL
        ),
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False
    )
