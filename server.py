from __future__ import annotations

import json
import os
import re
import time
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


def extract_matriculas(value):
    if not value:
        return []

    found = re.findall(
        r"(?<!\d)\d{6}\s*[-/.]\s*\d(?!\d)"
        r"|"
        r"(?<!\d)\d{6}\s+\d(?!\d)",
        str(value)
    )

    return [clean(x).replace(" ", "") for x in found]


def extract_function(value):
    m = re.search(r"\((CMT|PAT|MOT)\)", value or "", re.I)
    return m.group(1).upper() if m else ""


def strip_function(value):
    return clean(
        re.sub(
            r"\s*\((?:CMT|PAT|MOT)\)\s*",
            "",
            value or "",
            flags=re.I
        )
    )


def extract_vehicle(value):
    found = re.findall(
        r"(?:M\.?\s*O\.?|GT|PCR)\s*\d+(?:\.\d+)?",
        value or "",
        re.I
    )
    return ", ".join(dict.fromkeys(found))


def load_data():
    if not DATA_FILE.exists():
        return None

    try:
        return json.loads(
            DATA_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return None


MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

def document_period(document):
    title = clean(document.get("titulo", ""))
    m = re.search(r"(janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(20\d{2})", title, re.I)
    if m:
        return MONTHS[m.group(1).lower()], int(m.group(2))
    for value in (document.get("documento_data", ""), document.get("data_protocolo", "")):
        m = re.search(r"(\d{2})/(\d{2})/(20\d{2})", str(value or ""))
        if m:
            return int(m.group(2)), int(m.group(3))
    return None, None

def full_date_from_day(day, document):
    m = re.search(r"\b(0?[1-9]|[12]\d|3[01])\b", clean(day))
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

def classify_occurrence(document, text):
    title = clean(document.get("titulo", ""))
    upper = (title + " " + text).upper()
    if re.search(r"ESCALA\s+PRINCIPAL", title, re.I):
        return "Escala Principal"
    if "PERMUTA" in upper:
        return "Permuta de Serviço"
    if "FOLGA" in upper or "CONCESSÃO DO CMT" in upper or "CONCESÃO DO CMT" in upper:
        return "Escala de Folga"
    if "COMPENSAÇÃO" in upper or "COMPENSACAO" in upper:
        return "Escala de Serviço"
    return "Escala de Serviço"

def infer_row(row, matricula, document):
    cells = [clean(x) for x in (row or [])]
    text = " | ".join(cells)

    if norm(matricula) not in norm(text):
        return None

    row_day = cells[6] if len(cells) >= 7 else ""
    row_date = full_date_from_day(row_day, document)
    out = {
        "data": row_date or (
            document.get("documento_data")
            or document.get("data_protocolo", "")
        ),
        "servico": "",
        "equipe": "",
        "graduacao": "",
        "matricula": matricula,
        "nome": "",
        "funcao": "",
        "viatura": "",
        "horario": "",
        "dias": "",
        "observacao": "",
        "situacao": "",
        "contexto": text,
    }

    if len(cells) >= 8:
        out["servico"] = cells[0]
        out["equipe"] = cells[1]
        out["graduacao"] = cells[3]
        out["horario"] = cells[7]
        out["dias"] = cells[6]
        out["viatura"] = extract_vehicle(cells[0])

        mats = extract_matriculas(cells[4])
        names = split_items(cells[5])
        grades = split_items(cells[3])

        idx = next(
            (
                i
                for i, value in enumerate(mats)
                if norm(value) == norm(matricula)
            ),
            None
        )

        if idx is not None:
            if idx < len(names):
                out["nome"] = strip_function(names[idx])
                out["funcao"] = extract_function(names[idx])

            if idx < len(grades):
                out["graduacao"] = grades[idx]

    if not out["horario"]:
        m = re.search(
            r"\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b",
            text,
            re.I
        )
        if m:
            out["horario"] = m.group(0)

    if not out["data"]:
        m = re.search(
            r"\b\d{2}/\d{2}/\d{4}\b",
            text
        )
        if m:
            out["data"] = m.group(0)

    if not out["funcao"]:
        out["funcao"] = extract_function(text)

    if not out["nome"]:
        m = re.search(
            r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{2,})\s*"
            r"\((CMT|PAT|MOT)\)",
            text,
            re.I
        )
        if m:
            out["nome"] = clean(m.group(1))

    if not out["viatura"]:
        out["viatura"] = extract_vehicle(text)

    tipo = classify_occurrence(document, text)
    out["tipo_documento"] = tipo
    if tipo == "Escala de Folga":
        out["situacao"] = "Folga"
    elif tipo == "Permuta de Serviço":
        out["situacao"] = "Permuta"
    elif "COMPENSAÇÃO" in text.upper() or "COMPENSACAO" in text.upper():
        out["situacao"] = "Compensação"
    else:
        out["situacao"] = "Escala"

    return out


def find_candidates(matricula):
    data = load_data()

    if not data:
        return None, []

    results = []

    for document in data.get("documentos", []):
        occurrences = []

        for row in document.get("rows", []):
            hit = infer_row(
                row,
                matricula,
                document
            )

            if hit:
                occurrences.append(hit)

        if occurrences:
            results.append({
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
                "ocorrencias": occurrences,
            })

    return data, results


def basic_profile(results):
    profile = {
        "nome": "",
        "graduacao": "",
        "funcao": "",
        "servico": "",
        "viatura": "",
        "equipe": "",
        "horario": "",
        "jornada": "24 x 72",
    }

    for result in results:
        for occurrence in result["ocorrencias"]:
            for key in profile:
                if (
                    not profile[key]
                    and occurrence.get(key)
                ):
                    profile[key] = occurrence[key]

    return profile


def get_gemini_client():
    key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY não está configurada no Render."
        )

    return genai.Client(api_key=key)


def analyze_with_ai(matricula, documents):
    payload = []

    for document in documents:
        payload.append({
            "id": document["id"],
            "titulo": document["titulo"],
            "data_protocolo": document["data_protocolo"],
            "documento_data": document["documento_data"],
            "ocorrencias": document["ocorrencias"],
        })

    prompt = f"""
Analise SOMENTE os dados fornecidos abaixo para a matrícula
{matricula}.

Não invente informações e não use informações de outras matrículas.

Objetivo:
organizar os registros encontrados e corrigir a classificação
quando o próprio conteúdo do documento deixar isso claro.

Regras importantes:

1. ESCALA PRINCIPAL
- Identifique os dias efetivamente associados à matrícula.
- Não complete dias por inferência.
- Não copie dias de outra pessoa.
- Se o documento tiver texto truncado, use somente o que estiver
  claramente associado à matrícula.
- Preserve o horário encontrado no documento.

2. ESCALA DE FOLGA
- Se houver indicação de folga, "concessão do CMT" ou documento
  claramente referente à folga, classifique como "Folga".
- Preserve a data específica e o horário específico.
- Não transforme uma folga em escala regular.

3. OUTROS REGISTROS
- Preserve permuta, compensação e retificação quando estiverem
  claramente indicadas.
- Não misture informações de documentos diferentes.

4. PERFIL
- O nome, graduação, função, serviço, viatura e equipe devem
  corresponder à matrícula consultada.
- Jornada padrão: "24 x 72", salvo informação diferente explícita.

5. IMPORTANTE
- Cada ocorrência deve permanecer vinculada ao seu documento.
- Não invente datas.
- Não invente viatura.
- Não invente equipe.
- Não invente horários.
- Quando um campo não puder ser determinado, deixe vazio.

Responda SOMENTE JSON, exatamente neste formato:

{{
  "perfil": {{
    "nome": "",
    "graduacao": "",
    "funcao": "",
    "servico": "",
    "viatura": "",
    "equipe": "",
    "horario": "",
    "jornada": "24 x 72"
  }},
  "ocorrencias": [
    {{
      "documento": "",
      "titulo": "",
      "tipo": "",
      "data": "",
      "servico": "",
      "equipe": "",
      "graduacao": "",
      "matricula": "{matricula}",
      "nome": "",
      "funcao": "",
      "viatura": "",
      "horario": "",
      "dias": "",
      "observacao": ""
    }}
  ]
}}

DADOS:
{json.dumps(payload, ensure_ascii=False)}
"""

    client = get_gemini_client()

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config={
            "temperature": 0,
            "response_mime_type": "application/json",
        },
    )

    return json.loads(response.text)


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
        return {"ok": False, "codigo": "MATRICULA_INVALIDA", "erro": "Digite uma matrícula válida, por exemplo 113260-1."}

    data = load_data()
    if not data or not data.get("documentos"):
        return {"ok": False, "codigo": "BASE_AINDA_NAO_ATUALIZADA", "erro": "A base de escalas ainda não foi atualizada."}

    resultados = []
    for document in data.get("documentos", []):
        occurrences = []
        for row in document.get("rows", []):
            hit = infer_row(row, original, document)
            if hit:
                occurrences.append(hit)
        if not occurrences:
            continue
        occurrences.sort(key=lambda o: date_sort_value(o.get("data", "")))
        groups = {}
        for occurrence in occurrences:
            tipo = occurrence.get("tipo_documento", "Escala de Serviço")
            groups.setdefault(tipo, []).append(occurrence)
        for tipo, items in groups.items():
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

    resultados.sort(key=lambda r: (result_priority(r), min([date_sort_value(o.get("data", "")) for o in r["ocorrencias"]], default=(9999,99,99)), r.get("id", "")))

    profile = basic_profile(resultados)
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
        "version": "7.0-deterministico",
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
            "mensagem": (
                "Base ainda não atualizada."
            ),
        })

    return jsonify({
        "ok": True,
        "base_pronta": bool(
            data.get("documentos")
        ),
        "version": "7.0-deterministico",
        "processo": data.get(
            "processo",
            ""
        ),
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

    return jsonify({
        "ok": True,
        "base_pronta": bool(
            data.get("documentos")
        ),
        "mensagem": (
            "O servidor está funcionando e "
            "a base local de escalas está disponível."
        ),
        "documentos": len(
            data.get("documentos", [])
        ),
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
            os.environ.get("PORT", 5000)
        ),
        debug=False
    )

