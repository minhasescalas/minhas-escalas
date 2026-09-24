from __future__ import annotations

import json
import os
import re
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from google import genai

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


def infer_row(row, matricula, document):
    cells = [clean(x) for x in (row or [])]
    text = " | ".join(cells)

    if norm(matricula) not in norm(text):
        return None

    out = {
        "data": (
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

    lower = (
        document.get("titulo", "")
        + " "
        + text
    ).lower()

    if "folga" in lower or "concessão do cmt" in lower:
        out["situacao"] = "Folga"
    elif "permuta" in lower:
        out["situacao"] = "Permuta"
    elif "retific" in lower:
        out["situacao"] = "Retificação"
    elif "compensa" in lower:
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

    data, documents = find_candidates(original)

    if not data or not data.get("documentos"):
        return {
            "ok": False,
            "codigo": "BASE_AINDA_NAO_ATUALIZADA",
            "erro": (
                "A base de escalas ainda não está "
                "disponível."
            ),
        }

    if not documents:
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
            "documentos_com_ocorrencia": 0,
            "perfil": {
                "nome": "",
                "graduacao": "",
                "funcao": "",
                "servico": "",
                "viatura": "",
                "equipe": "",
                "horario": "",
                "jornada": "24 x 72",
            },
            "resultados": [],
            "fonte": data.get(
                "fonte",
                PROCESS_URL
            ),
            "atualizado_em": data.get(
                "atualizado_em",
                ""
            ),
        }

    try:
        ai_result = analyze_with_ai(
            original,
            documents
        )

        by_id = {
            str(document["id"]): document
            for document in documents
        }

        grouped = {}

        for item in ai_result.get(
            "ocorrencias",
            []
        ):
            source = by_id.get(
                str(item.get("documento", ""))
            )

            if not source:
                continue

            tipo = clean(item.get("tipo", ""))

            situation_map = {
                "Folga": "Folga",
                "Escala de Folga": "Folga",
                "Permuta": "Permuta",
                "Compensação": "Compensação",
                "Retificação": "Retificação",
            }

            situation = situation_map.get(
                tipo,
                "Escala"
            )

            occurrence = {
                key: clean(item.get(key, ""))
                for key in (
                    "data",
                    "servico",
                    "equipe",
                    "graduacao",
                    "nome",
                    "funcao",
                    "viatura",
                    "horario",
                    "dias",
                    "observacao",
                )
            }

            occurrence["matricula"] = original
            occurrence["situacao"] = situation
            occurrence["tipo"] = tipo

            if source["id"] not in grouped:
                grouped[source["id"]] = {
                    **source,
                    "ocorrencias": [],
                }

            grouped[source["id"]][
                "ocorrencias"
            ].append(occurrence)

        results = list(grouped.values())

        if not results:
            results = documents

        ai_profile = ai_result.get("perfil") or {}

        profile = {
            "nome": clean(
                ai_profile.get("nome", "")
            ),
            "graduacao": clean(
                ai_profile.get("graduacao", "")
            ),
            "funcao": clean(
                ai_profile.get("funcao", "")
            ),
            "servico": clean(
                ai_profile.get("servico", "")
            ),
            "viatura": clean(
                ai_profile.get("viatura", "")
            ),
            "equipe": clean(
                ai_profile.get("equipe", "")
            ),
            "horario": clean(
                ai_profile.get("horario", "")
            ),
            "jornada": clean(
                ai_profile.get(
                    "jornada",
                    "24 x 72"
                )
            ),
        }

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
            "documentos_com_ocorrencia": len(
                results
            ),
            "perfil": profile,
            "resultados": results,
            "fonte": data.get(
                "fonte",
                PROCESS_URL
            ),
            "atualizado_em": data.get(
                "atualizado_em",
                ""
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "codigo": "GEMINI_ERRO",
            "erro": str(exc),
            "matricula": original,
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
        "version": "6.0-gemini",
        "base_pronta": bool(
            data and data.get("documentos")
        ),
        "gemini_configurado": bool(
            os.environ.get("GEMINI_API_KEY")
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
        "version": "6.0-gemini",
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
        "gemini_configurado": bool(
            os.environ.get("GEMINI_API_KEY")
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
        "gemini_configurado": bool(
            os.environ.get("GEMINI_API_KEY")
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
