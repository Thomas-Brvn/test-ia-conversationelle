"""Agent conversationnel : Mistral (le cerveau) + un serveur MCP (les mains).

Deux serveurs MCP possibles, au choix :
  - "local"   : MCP Toolbox for Databases, lancé sur la machine. Expose NOS outils
                (kpi_*, fiche_client…) définis dans mcp/tools.*.yaml.
  - "distant" : serveur MCP BigQuery hébergé par Google (rien à installer).
                Outils génériques uniquement ; la couche sémantique passe par le prompt.

Boucle :
  1. On récupère la liste des outils exposés par le serveur MCP.
  2. On envoie à Mistral : couche sémantique + question + liste des outils.
  3. Mistral répond soit par du texte (fini), soit par "appelle l'outil X avec Y".
  4. On exécute l'appel via MCP, on renvoie le résultat à Mistral, et on recommence.
"""

import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import google.auth
import google.auth.transport.requests
import httpx2
import yaml
from dotenv import load_dotenv
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mistralai.client import Mistral

RACINE = Path(__file__).parent.parent
load_dotenv(RACINE / ".env")

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:5000/mcp/banque")
MCP_URL_GOOGLE = "https://bigquery.googleapis.com/mcp"
MODELE = os.environ.get("MISTRAL_MODEL", "ministral-14b-latest")
DIALECTE = os.environ.get("DIALECTE", "bigquery")  # "bigquery" ou "sqlite"
PROJET = os.environ.get("GCP_PROJECT", "")
DATASET = os.environ.get("BQ_DATASET", "banque_demo")
MAX_TOURS = 8              # nombre max d'allers-retours avec les outils
MAX_CARACTERES_RESULTAT = 12000
MAX_OCTETS_SCANNES = 104857600  # 100 Mo par requête en mode distant

MODES = {"local": "MCP local (Toolbox)"}
if DIALECTE == "bigquery":
    MODES["distant"] = "MCP distant Google (hébergé)"
MODE_DEFAUT = os.environ.get("MCP_MODE", "local")

SQL_LECTURE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
SQL_ECRITURE = re.compile(r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant)\b", re.IGNORECASE)
REF_TABLE = re.compile(r"\b(?:from|join)\s+`?([\w.-]+)`?", re.IGNORECASE)

# Mode distant : seuls ces outils (lecture seule) sont montrés à Mistral, avec un schéma simplifié.
# projectId / datasetId sont imposés par l'app : l'IA ne peut pas viser un autre projet.
OUTILS_DISTANTS = {
    "list_table_ids": ("Liste les tables du dataset banque_demo.", {}),
    "get_table_info": ("Renvoie les colonnes et types d'une table du dataset banque_demo.",
                       {"tableId": {"type": "string", "description": "Nom de la table, ex. clients"}}),
    "execute_sql_readonly": ("Exécute une requête SELECT (GoogleSQL / BigQuery). Tables préfixées par banque_demo.",
                             {"query": {"type": "string", "description": "Requête SELECT GoogleSQL"}}),
}


@dataclass
class Etape:
    outil: str
    arguments: dict
    lignes: list = field(default_factory=list)
    erreur: str | None = None


@dataclass
class Reponse:
    texte: str
    etapes: list[Etape]


# ---------- Prompt ----------

def requetes_de_reference() -> list[dict]:
    """Les indicateurs SQL validés de tools.bigquery.yaml, réutilisés comme exemples en mode distant."""
    documents = yaml.safe_load_all((RACINE / "mcp" / "tools.bigquery.yaml").read_text(encoding="utf-8"))
    return [
        {"indicateur": d["name"], "definition": " ".join(d["description"].split()),
         "parametres": [p["name"] for p in d.get("parameters", [])], "sql": d["statement"]}
        for d in documents if d and d.get("kind") == "tool" and "statement" in d
    ]


def prompt_systeme(mode: str) -> str:
    semantique = yaml.safe_load((RACINE / "semantique" / "couche_semantique.yaml").read_text(encoding="utf-8"))
    prefixe = "banque_demo." if DIALECTE == "bigquery" else ""
    dialecte = semantique.pop("dialectes")[DIALECTE]
    for ex in semantique["exemples"]:
        ex["sql"] = ex["sql"].replace("{p}", prefixe)

    if mode == "local":
        consigne_outils = ("Si un outil métier (kpi_encours_par_agence, kpi_taux_impayes, depenses_par_categorie, "
                           "fiche_client) répond à la question, utilise-le en priorité plutôt que du SQL libre.")
        reference = ""
    else:
        consigne_outils = ("Si une requête de référence ci-dessous répond à la question, réutilise son SQL tel quel "
                           "via execute_sql_readonly (remplace les @parametres par des valeurs littérales) : "
                           "ce sont les calculs officiels validés par le métier.")
        reference = ("\n## Requêtes de référence validées\n"
                     + yaml.safe_dump(requetes_de_reference(), allow_unicode=True, sort_keys=False))

    return (
        "Tu es l'assistant data d'une banque. Tu réponds en français à des questions métier "
        "en interrogeant la base via les outils à ta disposition.\n\n"
        "Méthode : 1) repère dans la couche sémantique les indicateurs et tables concernés ; "
        "2) utilise un calcul officiel s'il existe, sinon écris une requête SQL ; "
        "3) si une requête échoue, lis l'erreur, corrige et réessaie ; "
        "4) réponds de façon concise avec les chiffres clés, en précisant la définition utilisée. "
        "Ne jamais inventer de chiffre : tout chiffre doit venir d'un résultat d'outil.\n"
        f"{consigne_outils}\n\n"
        f"## Moteur SQL\n{dialecte}\n"
        "## Couche sémantique\n"
        f"{yaml.safe_dump(semantique, allow_unicode=True, sort_keys=False)}"
        f"{reference}"
    )


# ---------- Connexion aux serveurs MCP ----------

@asynccontextmanager
async def connexion_mcp(mode: str):
    if mode == "local":
        async with Client(MCP_URL) as mcp:
            yield mcp
        return
    # Mode distant : authentification Google (compte connecté via gcloud / ADC)
    identifiants, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery"])
    identifiants.refresh(google.auth.transport.requests.Request())
    entetes = {"Authorization": f"Bearer {identifiants.token}", "x-goog-user-project": PROJET}
    async with httpx2.AsyncClient(headers=entetes, timeout=60) as http:
        async with Client(streamable_http_client(MCP_URL_GOOGLE, http_client=http)) as mcp:
            yield mcp


def outils_pour_mistral(outils, mode: str) -> list[dict]:
    """Traduit le format MCP en format 'function calling' de Mistral."""
    if mode == "local":
        definitions = [(o.name, o.description or "", o.input_schema) for o in outils]
    else:
        disponibles = {o.name for o in outils}
        definitions = [
            (nom, description, {"type": "object", "properties": proprietes, "required": list(proprietes)})
            for nom, (description, proprietes) in OUTILS_DISTANTS.items() if nom in disponibles
        ]
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, d, p in definitions]


def arguments_distants(outil: str, arguments: dict, schemas: dict) -> dict:
    """Ajoute les paramètres imposés par l'app (projet, dataset, étiquette de traçabilité)."""
    complets = {**arguments, "projectId": PROJET}
    if outil in ("list_table_ids", "get_table_info"):
        complets["datasetId"] = DATASET
    if outil == "execute_sql_readonly" and "labels" in schemas[outil].get("properties", {}):
        complets["labels"] = {"application": "poc-ia-banque"}
    return complets


async def refus_cout(mcp, arguments: dict) -> str | None:
    """Le serveur distant n'a pas de plafond de coût : on estime d'abord la requête (dry run, gratuit)."""
    estimation = await mcp.call_tool("execute_sql_readonly", {**arguments, "dryRun": True})
    if estimation.is_error:
        return None  # la vraie exécution renverra l'erreur SQL à Mistral
    texte = "".join(c.text for c in estimation.content if getattr(c, "text", None))
    octets = int(json.loads(texte).get("totalBytesProcessed", 0))
    if octets > MAX_OCTETS_SCANNES:
        return (f"Requête refusée : elle lirait {octets / 1e6:.0f} Mo (plafond {MAX_OCTETS_SCANNES / 1e6:.0f} Mo). "
                "Filtre davantage ou sélectionne moins de colonnes.")
    return None


# ---------- Lecture des résultats ----------

def convertir_valeur(valeur, type_bq: str):
    if valeur is None:
        return None
    if type_bq in ("INTEGER", "INT64"):
        return int(valeur)
    if type_bq in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        return round(float(valeur), 4)
    if type_bq in ("BOOLEAN", "BOOL"):
        return valeur == "true"
    return valeur


def lignes_bigquery(donnees: dict) -> list | None:
    """Le serveur Google renvoie le format brut de l'API BigQuery : on le remet en tableau lisible."""
    champs = donnees.get("schema", {}).get("fields")
    if not champs:
        return None
    if "rows" in donnees or "jobComplete" in donnees:  # résultat de requête
        return [{c["name"]: convertir_valeur(cellule.get("v"), c["type"]) for c, cellule in zip(champs, ligne["f"])}
                for ligne in donnees.get("rows", [])]
    return [{"colonne": c["name"], "type": c["type"]} for c in champs]  # description de table


def lire_resultat(resultat) -> tuple[list, str]:
    textes = [c.text for c in resultat.content if getattr(c, "text", None)]
    lignes = []
    for t in textes:
        try:
            valeur = json.loads(t)
        except json.JSONDecodeError:
            lignes.append({"resultat": t})
            continue
        converties = lignes_bigquery(valeur) if isinstance(valeur, dict) else None
        if converties is not None:
            lignes.extend(converties)
        else:
            lignes.extend(valeur if isinstance(valeur, list) else [valeur])
    if any("schema" in t for t in textes) and not resultat.is_error:
        return lignes, json.dumps(lignes, ensure_ascii=False, default=str)  # plus compact pour Mistral
    return lignes, "\n".join(textes)


def refus_garde_fou(outil: str, arguments: dict, mode: str) -> str | None:
    """Garde-fous côté app, en plus de ceux du serveur MCP."""
    sql = arguments.get("sql") or arguments.get("query") or ""
    if outil not in ("executer_sql", "execute_sql_readonly"):
        return None
    if not SQL_LECTURE.match(sql) or SQL_ECRITURE.search(sql):
        return "Requête refusée : seules les requêtes SELECT sont autorisées."
    if mode == "distant":
        hors_perimetre = [r for r in REF_TABLE.findall(sql) if "." in r and DATASET not in r.split(".")]
        if hors_perimetre:
            return f"Requête refusée : seul le dataset {DATASET} est autorisé ({', '.join(hors_perimetre)})."
    return None


# ---------- Boucle principale ----------

async def repondre(question: str, historique: list[dict], mode: str = MODE_DEFAUT) -> Reponse:
    """historique : [{"role": "user"|"assistant", "content": str}, ...] des tours précédents."""
    mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    etapes: list[Etape] = []

    async with connexion_mcp(mode) as mcp:
        outils_serveur = (await mcp.list_tools()).tools
        schemas = {o.name: o.input_schema for o in outils_serveur}
        outils = outils_pour_mistral(outils_serveur, mode)
        messages = [{"role": "system", "content": prompt_systeme(mode)}, *historique,
                    {"role": "user", "content": question}]

        for _ in range(MAX_TOURS):
            reponse = await mistral.chat.complete_async(
                model=MODELE, messages=messages, tools=outils, tool_choice="auto", temperature=0.1,
            )
            message = reponse.choices[0].message
            if not message.tool_calls:
                return Reponse(texte=message.content or "", etapes=etapes)

            messages.append(message)
            for appel in message.tool_calls:
                arguments = appel.function.arguments
                if isinstance(arguments, str):
                    arguments = json.loads(arguments or "{}")
                etape = Etape(outil=appel.function.name, arguments=arguments)
                etapes.append(etape)

                if refus := refus_garde_fou(etape.outil, arguments, mode):
                    etape.erreur = contenu = refus
                elif mode == "distant" and etape.outil not in OUTILS_DISTANTS:
                    etape.erreur = contenu = f"Outil {etape.outil} non autorisé."
                else:
                    if mode == "distant":
                        arguments = arguments_distants(etape.outil, arguments, schemas)
                    try:
                        refus = None
                        if mode == "distant" and etape.outil == "execute_sql_readonly":
                            refus = await refus_cout(mcp, arguments)
                        if refus:
                            etape.erreur = contenu = refus
                        else:
                            resultat = await mcp.call_tool(etape.outil, arguments)
                            etape.lignes, contenu = lire_resultat(resultat)
                            if resultat.is_error:
                                etape.erreur, etape.lignes = contenu, []
                    except MCPError as exc:  # certaines erreurs GCP remontent ainsi (ex. 403)
                        etape.erreur = contenu = f"Erreur du serveur MCP : {exc}"
                    contenu = contenu or "Aucune ligne."

                messages.append({
                    "role": "tool",
                    "name": etape.outil,
                    "tool_call_id": appel.id,
                    "content": contenu[:MAX_CARACTERES_RESULTAT],
                })

    return Reponse(texte="Je n'ai pas réussi à répondre en un nombre raisonnable d'étapes.", etapes=etapes)
