from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "sei_cache.json"
# Also accept the cache in the project root, which is convenient for Render/GitHub.
if not DATA_FILE.exists():
    DATA_FILE = BASE_DIR / "sei_cache.json"

PDF_DIRS = [
    BASE_DIR / "data" / "escalas27",
    BASE_DIR / "data" / "pdfs",
    BASE_DIR / "escalas27",
    BASE_DIR / "pdfs",
    BASE_DIR,
]

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")
OPENAI_CACHE_FILE = BASE_DIR / ".openai_file_ids.json"

PROCESS_URL = (
    "https://sei.pe.gov.br/sei/processo_acesso_externo_consulta.php"
    "?id_acesso_externo=709257"
    "&infra_hash=9d48ee84267766a3f78448f3246b30bf"
)

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")

MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
}


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\D", "", str(value or ""))


def matricula_pattern(matricula):
    target = norm(matricula)
    if len(target) < 5:
        return ""
    if len(target) == 7:
        return rf"(?<!\d){re.escape(target[:6])}\s*[-/.]?\s*{re.escape(target[6])}(?!\d)"
    return rf"(?<!\d){re.escape(target)}(?!\d)"


def matricula_matches(text, matricula):
    p = matricula_pattern(matricula)
    return bool(p and re.search(p, str(text or ""), re.I))


def load_data():
    if not DATA_FILE.exists():
        return None
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_documents_for_matricula(data, matricula):
    """Use the cache only to locate candidate PDFs; the AI reads the PDFs themselves."""
    candidates = []
    for document in data.get("documentos", []):
        hit = False
        for row in document.get("rows", []):
            cells = [str(x or "") for x in (row or [])]
            # Prefer exact matricula column, but accept the row/context when necessary.
            if len(cells) > 4 and norm(cells[4]) == norm(matricula):
                hit = True
                break
            if matricula_matches(" | ".join(cells), matricula):
                hit = True
                break
        if hit:
            candidates.append(document)
    return candidates


def all_pdf_files():
    files = []
    seen = set()
    for directory in PDF_DIRS:
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            paths = directory.rglob("*.pdf")
        except Exception:
            continue
        for path in paths:
            try:
                key = str(path.resolve())
            except Exception:
                key = str(path)
            if key not in seen:
                seen.add(key)
                files.append(path)
    return files


def find_pdf_for_document(document):
    wanted = clean(document.get("arquivo", ""))
    doc_id = clean(document.get("id", ""))

    files = all_pdf_files()
    if wanted:
        wanted_base = Path(wanted).name
        for path in files:
            if path.name == wanted_base:
                return path
            # Cache filenames sometimes contain (1) while deployment files do not.
            if path.stem == Path(wanted_base).stem.replace("(1)", ""):
                return path

    if doc_id:
        for path in files:
            if doc_id in path.name:
                return path

    # Last resort: the combined September PDF can contain the whole process.
    for path in files:
        if path.name.lower() == "tatico escalas de setembro.pdf":
            return path
    return None


def load_openai_file_cache():
    if not OPENAI_CACHE_FILE.exists():
        return {}
    try:
        value = json.loads(OPENAI_CACHE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_openai_file_cache(cache):
    try:
        OPENAI_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def get_client():
    if OpenAI is None:
        raise RuntimeError("A biblioteca 'openai' não está instalada no servidor.")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não está configurada no Render.")
    return OpenAI(api_key=api_key)


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "matricula": {"type": "string"},
        "perfil": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "nome": {"type": "string"},
                "graduacao": {"type": "string"},
                "funcao": {"type": "string"},
                "servico": {"type": "string"},
                "viatura": {"type": "string"},
                "equipe": {"type": "string"},
                "horario": {"type": "string"},
                "jornada": {"type": "string"},
            },
            "required": ["nome", "graduacao", "funcao", "servico", "viatura", "equipe", "horario", "jornada"],
        },
        "resultados": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "titulo": {"type": "string"},
                    "tipo_documento": {"type": "string"},
                    "titulo_exibicao": {"type": "string"},
                    "ocorrencias": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "data": {"type": "string"},
                                "servico": {"type": "string"},
                                "equipe": {"type": "string"},
                                "graduacao": {"type": "string"},
                                "matricula": {"type": "string"},
                                "nome": {"type": "string"},
                                "funcao": {"type": "string"},
                                "viatura": {"type": "string"},
                                "horario": {"type": "string"},
                                "dias": {"type": "string"},
                                "observacao": {"type": "string"},
                                "situacao": {"type": "string"},
                                "tipo_documento": {"type": "string"},
                                "titulo_exibicao": {"type": "string"},
                            },
                            "required": [
                                "data", "servico", "equipe", "graduacao", "matricula",
                                "nome", "funcao", "viatura", "horario", "dias",
                                "observacao", "situacao", "tipo_documento", "titulo_exibicao",
                            ],
                        },
                    },
                },
                "required": ["id", "titulo", "tipo_documento", "titulo_exibicao", "ocorrencias"],
            },
        },
    },
    "required": ["ok", "matricula", "perfil", "resultados"],
}


INSTRUCTIONS = r"""
Você é o extrator oficial de dados do sistema Minhas Escalas.

Sua única fonte de verdade são os PDFs de escalas anexados nesta consulta.
O servidor anexará um ou mais PDFs e informará abaixo o ID/título esperado de cada arquivo.

OBJETIVO
Localizar EXATAMENTE a matrícula solicitada nos PDFs e devolver os registros que pertencem a essa matrícula.

REGRA MAIS IMPORTANTE
NÃO invente, complete, estime, calcule ou deduza informação que não esteja sustentada pelo PDF.
Não use conhecimento externo.
Não copie dias, horário, serviço, viatura ou situação de outra pessoa apenas porque parecem semelhantes.

ESCALA PRINCIPAL
A Escala Principal pode ter tabelas com células mescladas, grupos/equipes (ALFA, BRAVO, CHARLIE, DELTA) e dias escritos em uma célula que visualmente se aplica a várias linhas.
Você deve interpretar a posição visual da tabela e associar o grupo/dias à linha da matrícula.
Se os dias do grupo estiverem visualmente apresentados ao lado de várias pessoas, eles pertencem às pessoas daquele grupo conforme a estrutura da tabela.
NÃO use a ordem numérica da matrícula como substituto da leitura da tabela.
NÃO pegue os dias do grupo anterior ou seguinte.

OUTROS DOCUMENTOS
- Concessão do CMT / Compensação de Horas: classifique como "Escala de Folga" e situação "Folga" quando isso for o que o documento indicar.
- Registro de serviço efetivo: classifique como "Escala de Serviço".
- Permuta: classifique como "Permuta de Serviço".
- A classificação deve vir do conteúdo do documento, não do nome da pessoa.

DATAS
Em documentos mensais, transforme o dia em data completa usando o mês/ano indicado no próprio documento.
Ex.: dia 05 em setembro de 2026 -> 05/09/2026.
Só faça essa transformação quando o mês/ano estiver explícito no documento.

HORÁRIOS
Preserve o horário real da linha. Não troque o horário de uma pessoa pelo horário de outra.

CAMPOS
Retorne nome, graduação, função, serviço, viatura, equipe, horário, dias, data e observação somente quando estiverem sustentados pelo PDF.
Quando um campo não estiver identificável, retorne string vazia.

DUPLICATAS
Não crie duplicatas artificiais. Se a mesma matrícula aparecer mais de uma vez no mesmo documento em registros distintos, mantenha os registros distintos quando eles realmente forem distintos.

SAÍDA
Responda exclusivamente no JSON estruturado solicitado pelo sistema.
"""


def upload_or_get_file_id(client, path, cache):
    key = f"{path.resolve()}::{path.stat().st_size}::{path.stat().st_mtime_ns}"
    cached = cache.get(key)
    if cached:
        try:
            client.files.retrieve(cached)
            return cached
        except Exception:
            cache.pop(key, None)

    with path.open("rb") as fh:
        uploaded = client.files.create(
        file=fh,
        purpose="user_data",
        expires_after={"anchor": "created_at", "seconds": 2592000},
    )
    cache[key] = uploaded.id
    save_openai_file_cache(cache)
    return uploaded.id


def ai_consult(matricula):
    data = load_data()
    if not data or not data.get("documentos"):
        raise RuntimeError("A base de documentos ainda não está disponível.")

    candidates = find_documents_for_matricula(data, matricula)
    if not candidates:
        return {
            "ok": True,
            "matricula": matricula,
            "perfil": {k: ("24 × 72" if k == "jornada" else "") for k in (
                "nome", "graduacao", "funcao", "servico", "viatura", "equipe", "horario", "jornada"
            )},
            "resultados": [],
            "documentos_com_ocorrencia": 0,
            "fonte": data.get("fonte", PROCESS_URL),
            "atualizado_em": data.get("atualizado_em", ""),
        }

    # One combined PDF is enough if it is the only source available. Otherwise prefer
    # the exact PDFs referenced by the cache, which keeps the model focused.
    selected = []
    seen = set()
    for document in candidates:
        path = find_pdf_for_document(document)
        if not path:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        selected.append((document, path))

    if not selected:
        raise RuntimeError(
            "Encontrei a matrícula na base, mas os PDFs originais não estão no servidor. "
            "Coloque os PDFs das escalas em data/escalas27/ ou data/pdfs/."
        )

    client = get_client()
    file_cache = load_openai_file_cache()
    contents = []

    metadata_lines = [
        f"MATRÍCULA SOLICITADA: {matricula}",
        "DOCUMENTOS CANDIDATOS:",
    ]

    for document, path in selected:
        file_id = upload_or_get_file_id(client, path, file_cache)
        metadata_lines.append(
            f"- arquivo={path.name}; id_documento={document.get('id','')}; "
            f"titulo={document.get('titulo','')}; arquivo_cache={document.get('arquivo','')}"
        )
        contents.append({"type": "input_file", "file_id": file_id})

    contents.append({
        "type": "input_text",
        "text": (
            "Analise os PDFs anexados. "
            "A matrícula a localizar é exatamente " + matricula + ".\n\n" +
            "\n".join(metadata_lines) +
            "\n\nAssocie cada resultado ao id_documento/título correspondente informado acima."
        ),
    })

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=INSTRUCTIONS,
        input=[{"role": "user", "content": contents}],
        text={
            "format": {
                "type": "json_schema",
                "name": "minhas_escalas_result",
                "strict": True,
                "schema": SCHEMA,
            }
        },
    )

    raw = response.output_text
    try:
        result = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"A IA retornou uma resposta inválida: {exc}") from exc

    # Server-side metadata that the model should not invent.
    result["ok"] = True
    result["matricula"] = matricula
    result["documentos_com_ocorrencia"] = len(result.get("resultados", []))
    result["fonte"] = data.get("fonte", PROCESS_URL)
    result["atualizado_em"] = data.get("atualizado_em", "")
    result["ai"] = True
    result["ai_model"] = OPENAI_MODEL

    # Attach known URLs/titles from the cache by document ID.
    by_id = {str(d.get("id", "")): d for d, _ in selected}
    for item in result.get("resultados", []):
        doc = by_id.get(str(item.get("id", "")))
        if doc:
            item["titulo"] = doc.get("titulo", item.get("titulo", ""))
            item["url"] = doc.get("url", "")
            item["data_protocolo"] = doc.get("data_protocolo", "")
            item["documento_data"] = doc.get("documento_data", "")

    return result


# ----------------------------- HTTP -----------------------------

@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    data = load_data()
    return jsonify({
        "ok": True,
        "service": "minhas-escalas-ai",
        "version": "9.0-ai",
        "base_pronta": bool(data and data.get("documentos")),
        "documentos": data.get("documentos_acessiveis", 0) if data else 0,
        "ia_configurada": bool(os.environ.get("OPENAI_API_KEY")),
        "modelo": OPENAI_MODEL,
        "atualizado_em": data.get("atualizado_em", "") if data else "",
    })


@app.get("/api/consultar")
def api_consultar():
    matricula = clean(request.args.get("matricula", ""))
    if len(norm(matricula)) < 5:
        return jsonify({
            "ok": False,
            "codigo": "MATRICULA_INVALIDA",
            "erro": "Digite uma matrícula válida, por exemplo 113260-1.",
        }), 400

    try:
        return jsonify(ai_consult(matricula))
    except Exception as exc:
        return jsonify({
            "ok": False,
            "codigo": "IA_CONSULTA_ERRO",
            "erro": str(exc),
        }), 503


@app.get("/api/status")
def api_status():
    data = load_data()
    return jsonify({
        "ok": bool(data),
        "base_pronta": bool(data and data.get("documentos")),
        "version": "9.0-ai",
        "processo": data.get("processo", "") if data else "",
        "total_registros_processo": data.get("total_registros_processo", 0) if data else 0,
        "documentos_acessiveis": data.get("documentos_acessiveis", 0) if data else 0,
        "documentos_com_falha": data.get("documentos_com_falha", 0) if data else 0,
        "atualizado_em": data.get("atualizado_em", "") if data else "",
        "ia_configurada": bool(os.environ.get("OPENAI_API_KEY")),
        "modelo": OPENAI_MODEL,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
