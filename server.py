from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "sei_cache.json"
PROCESS_URL = "https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php?id_acesso_externo=709257&infra_hash=9d48ee84267766a3f78448f3246b30bf"

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def norm(value):
    return re.sub(r"\D", "", value or "")


def matricula_matches(text, matricula):
    target = norm(matricula)
    if len(target) < 5:
        return False
    text = text or ""
    compact = norm(text)
    if target not in compact:
        return False
    # Prefer a real matrícula token instead of a substring inside another number.
    variants = [
        rf"(?<!\d){re.escape(target[:6])}\s*[-/.]?\s*{re.escape(target[6:])}(?!\d)"
        if len(target) > 6 else rf"(?<!\d){re.escape(target)}(?!\d)"
    ]
    return any(re.search(v, text, re.I) for v in variants)


def split_items(value):
    if not value:
        return []
    value = value.replace("\r", "\n")
    parts = re.split(r"\s*/\s*|\s*\|\s*|\s*;\s*|\n+", value)
    return [clean(x) for x in parts if clean(x)]


def extract_matriculas(value):
    if not value:
        return []
    found = re.findall(r"(?<!\d)\d{6}\s*[-/.]\s*\d(?!\d)|(?<!\d)\d{6}\s+\d(?!\d)", value)
    return [clean(x).replace(" ", "") for x in found]


def extract_function(name):
    m = re.search(r"\((CMT|PAT|MOT)\)", name or "", re.I)
    return m.group(1).upper() if m else ""


def strip_function(name):
    return clean(re.sub(r"\s*\((?:CMT|PAT|MOT)\)\s*", "", name or "", flags=re.I))


def extract_vehicle(service):
    found = re.findall(r"(?:M\.?O\.?|GT|PCR)\s*\d+(?:\.\d+)?", service or "", re.I)
    return ", ".join(dict.fromkeys(found))


def infer_row(row, matricula, document):
    cells = [clean(x) for x in (row or [])]
    text = " | ".join(cells)
    if not matricula_matches(text, matricula):
        return None

    out = {
        "data": document.get("documento_data") or document.get("data_protocolo", ""),
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

    # Standard SEI scale table: Serviço/Função | Equipe | Ord. | Graduação | Matrícula | Efetivo | Dias | Horário
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
        target = norm(matricula)
        idx = next((i for i, m in enumerate(mats) if norm(m) == target), None)
        if idx is not None:
            if idx < len(names):
                out["nome"] = strip_function(names[idx])
                out["funcao"] = extract_function(names[idx])
            if idx < len(grades):
                out["graduacao"] = grades[idx]

    # Fallbacks for irregular documents.
    if not out["horario"]:
        m = re.search(r"\b\d{1,2}h\d{0,2}\s*/\s*\d{1,2}h\d{0,2}\b", text, re.I)
        if m:
            out["horario"] = m.group(0)
    if not out["data"]:
        m = re.search(r"\b\d{2}/\d{2}/\d{4}\b", text)
        if m:
            out["data"] = m.group(0)
    if not out["funcao"]:
        out["funcao"] = extract_function(text)
    if not out["nome"]:
        m = re.search(r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{2,})\s*\((CMT|PAT|MOT)\)", text, re.I)
        if m:
            out["nome"] = clean(m.group(1))
    if not out["viatura"]:
        out["viatura"] = extract_vehicle(text)
    if "*" in text:
        m = re.search(r"\*[^|]{0,180}", text)
        if m:
            out["observacao"] = clean(m.group(0))
    if not out["observacao"] and re.search(r"folga|compensa|permuta|retificad|concessão|a/c", text, re.I):
        out["observacao"] = text[:220]

    lower = (document.get("titulo", "") + " " + text).lower()
    if "folga" in lower:
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


def load_data():
    if not DATA_FILE.exists():
        return None
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def consult(matricula):
    original = clean(matricula)
    n = norm(original)
    if len(n) < 5:
        return {"ok": False, "codigo": "MATRICULA_INVALIDA", "erro": "Digite uma matrícula válida, por exemplo 113260-1."}

    data = load_data()
    if not data or not data.get("documentos"):
        return {
            "ok": False,
            "codigo": "BASE_AINDA_NAO_ATUALIZADA",
            "erro": "A base de escalas ainda não foi atualizada. Tente novamente em alguns minutos.",
        }

    resultados = []
    for doc in data.get("documentos", []):
        ocorrencias = []
        for row in doc.get("rows", []):
            hit = infer_row(row, original, doc)
            if hit:
                ocorrencias.append(hit)
        if ocorrencias:
            resultados.append({
                "id": doc.get("id", ""),
                "titulo": doc.get("titulo", "Documento SEI"),
                "data_protocolo": doc.get("data_protocolo", ""),
                "documento_data": doc.get("documento_data", ""),
                "url": doc.get("url", ""),
                "ocorrencias": ocorrencias,
            })

    resultados.sort(key=lambda x: (x.get("documento_data") or "99/99/9999", x.get("id") or ""))
    profile = {"nome":"", "graduacao":"", "funcao":"", "servico":"", "viatura":"", "equipe":"", "horario":"", "jornada":"24 x 72"}
    for result in resultados:
        for occurrence in result["ocorrencias"]:
            for key in ("nome", "graduacao", "funcao", "servico", "viatura", "equipe", "horario"):
                if not profile[key] and occurrence.get(key):
                    profile[key] = occurrence[key]

    return {
        "ok": True,
        "matricula": original,
        "matricula_normalizada": n,
        "total_registros_processo": data.get("total_registros_processo", 0),
        "documentos_acessiveis": data.get("documentos_acessiveis", 0),
        "documentos_com_falha": data.get("documentos_com_falha", 0),
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
        "version": "5.0",
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
        return jsonify({"ok": False, "base_pronta": False, "mensagem": "Base ainda não atualizada."})
    return jsonify({
        "ok": True,
        "base_pronta": bool(data.get("documentos")),
        "version": 5,
        "processo": data.get("processo", ""),
        "total_registros_processo": data.get("total_registros_processo", 0),
        "documentos_acessiveis": data.get("documentos_acessiveis", 0),
        "documentos_com_falha": data.get("documentos_com_falha", 0),
        "atualizado_em": data.get("atualizado_em", ""),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
