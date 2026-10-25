"""Interface Streamlit.  Lancement : streamlit run app/app.py"""

import asyncio
import json
import uuid
from dataclasses import asdict
from datetime import datetime

import pandas as pd
import streamlit as st
import yaml

import agent

FICHIER_HISTORIQUE = agent.RACINE / "historique" / "conversations.json"

QUESTIONS_EXEMPLES = [  # (libellé du bouton, question envoyée)
    ("🏦 Encours par agence", "Quel est l'encours de dépôts et de crédits par agence ?"),
    ("⚠️ Taux d'impayés", "Quel type de crédit a le taux d'impayés le plus élevé ?"),
    ("📉 Clients à découvert", "Combien de clients sont à découvert, par segment ?"),
    ("👥 Âge des clients Premium", "Quel est l'âge moyen des clients Premium par région ?"),
]

st.set_page_config(page_title="Assistant data — Banque Démo", page_icon="🏦", layout="wide")


# ---------- Historique des conversations (sauvegardé sur disque) ----------

def charger_historique() -> dict:
    try:
        return json.loads(FICHIER_HISTORIQUE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def sauver_historique():
    FICHIER_HISTORIQUE.parent.mkdir(exist_ok=True)
    FICHIER_HISTORIQUE.write_text(
        json.dumps(st.session_state.conversations, ensure_ascii=False, indent=1), encoding="utf-8")


def nouvelle_conversation():
    st.session_state.conversation_id = None


st.session_state.setdefault("conversations", charger_historique())
st.session_state.setdefault("conversation_id", None)

conversations = st.session_state.conversations
conversation = conversations.get(st.session_state.conversation_id)
messages = conversation["messages"] if conversation else []


# ---------- Barre latérale : historique ----------

with st.sidebar:
    st.button("➕ Nouvelle conversation", on_click=nouvelle_conversation, width="stretch", type="primary")
    st.subheader("Historique")
    if not conversations:
        st.caption("Aucune conversation pour l'instant.")
    for cid, conv in sorted(conversations.items(), key=lambda c: c[1]["date"], reverse=True):
        col_titre, col_suppr = st.columns([5, 1])
        actif = cid == st.session_state.conversation_id
        if col_titre.button(("▸ " if actif else "") + conv["titre"], key=f"ouvrir_{cid}",
                            width="stretch", help=conv["date"][:16].replace("T", " à ")):
            st.session_state.conversation_id = cid
            st.rerun()
        if col_suppr.button("🗑", key=f"suppr_{cid}", help="Supprimer cette conversation"):
            del conversations[cid]
            if actif:
                st.session_state.conversation_id = None
            sauver_historique()
            st.rerun()
    st.divider()
    mode = st.radio("Serveur MCP", list(agent.MODES), format_func=agent.MODES.get, key="mode",
                    index=list(agent.MODES).index(agent.MODE_DEFAUT) if agent.MODE_DEFAUT in agent.MODES else 0,
                    help="Local : outils métier sur mesure, serveur à lancer. "
                         "Distant : hébergé par Google, rien à installer, outils génériques.")
    url = agent.MCP_URL if mode == "local" else agent.MCP_URL_GOOGLE
    st.caption(f"Moteur : `{agent.DIALECTE}` · Modèle : `{agent.MODELE}`  \nURL : `{url}`")


# ---------- En-tête + schéma des données ----------

st.title("🏦 Assistant data — Banque Démo")
st.caption("Données 100 % fictives · Mistral + serveur MCP (MCP Toolbox for Databases)")


@st.cache_data
def lire_semantique() -> dict:
    return yaml.safe_load((agent.RACINE / "semantique" / "couche_semantique.yaml").read_text(encoding="utf-8"))


def schema_dot(semantique: dict) -> str:
    """Diagramme des tables (Graphviz) construit depuis la couche sémantique."""
    noeuds = []
    for table, infos in semantique["tables"].items():
        lignes = "".join(
            f'<TR><TD ALIGN="LEFT" PORT="{col}"><FONT POINT-SIZE="10">{col}</FONT></TD></TR>'
            for col in infos["colonnes"])
        noeuds.append(
            f'{table} [label=<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4">'
            f'<TR><TD BGCOLOR="#2563eb"><FONT COLOR="white"><B>{table}</B></FONT></TD></TR>{lignes}</TABLE>>];')
    liens = []
    for relation in semantique["relations"]:  # ex : "comptes.client_id = clients.client_id"
        gauche, droite = (cote.strip().split(".") for cote in relation.split("="))
        liens.append(f"{gauche[0]}:{gauche[1]} -> {droite[0]}:{droite[1]};")
    return (
        "digraph { rankdir=LR; bgcolor=transparent; "
        'node [shape=plaintext, fontname="Helvetica", fontcolor="#1f2937", style=filled, fillcolor="#f8fafc"]; '
        'edge [color="#94a3b8", arrowhead=crow, arrowsize=0.7]; '
        + " ".join(noeuds + liens) + " }"
    )


semantique = lire_semantique()
with st.expander("🗂️ Schéma des données", expanded=not messages):
    st.graphviz_chart(schema_dot(semantique), width="stretch")
    onglets = st.tabs(list(semantique["tables"]))
    for onglet, (table, infos) in zip(onglets, semantique["tables"].items()):
        with onglet:
            st.markdown(f"**{table}** — {infos['description']}")
            st.dataframe(
                pd.DataFrame([{"colonne": c, "signification": d} for c, d in infos["colonnes"].items()]),
                hide_index=True, width="stretch")


# ---------- Conversation ----------

def afficher_etapes(etapes):
    if not etapes:
        return
    with st.expander(f"🔧 Ce que l'IA a fait ({len(etapes)} appel(s) d'outil MCP)"):
        for i, e in enumerate(etapes, 1):
            st.markdown(f"**{i}. `{e['outil']}`**")
            sql = e["arguments"].get("sql") or e["arguments"].get("query")
            if sql:
                st.code(sql, language="sql")
            elif e["arguments"]:
                st.json(e["arguments"])
            if e["erreur"]:
                st.error(e["erreur"])
            elif e["lignes"]:
                st.dataframe(pd.DataFrame(e["lignes"]), hide_index=True)


for m in messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("mode"):
            st.caption(f"via {agent.MODES.get(m['mode'], m['mode'])}")
        afficher_etapes(m.get("etapes"))

# Exemples : uniquement avant le début de la conversation, juste au-dessus de la saisie
question_exemple = None
zone_exemples = st.empty()
if not messages:
    with zone_exemples.container():
        st.markdown("##### 💡 Pour commencer, essayez :")
        colonnes = st.columns(2)
        for i, (libelle, q) in enumerate(QUESTIONS_EXEMPLES):
            if colonnes[i % 2].button(libelle, key=f"exemple_{i}", help=q, width="stretch"):
                question_exemple = q

question = st.chat_input("Posez une question sur les données de la banque…") or question_exemple

if question:
    zone_exemples.empty()
    with st.chat_message("user"):
        st.markdown(question)

    if conversation is None:
        cid = uuid.uuid4().hex[:8]
        titre = question if len(question) <= 40 else question[:38] + "…"
        conversation = conversations[cid] = {"titre": titre, "date": datetime.now().isoformat(), "messages": []}
        st.session_state.conversation_id = cid
        messages = conversation["messages"]

    historique = [{"role": m["role"], "content": m["content"]} for m in messages[-6:]]
    messages.append({"role": "user", "content": question})
    sauver_historique()

    with st.chat_message("assistant"), st.spinner("Réflexion et interrogation de la base…"):
        try:
            reponse = asyncio.run(agent.repondre(question, historique, mode=mode))
            texte, etapes = reponse.texte, [asdict(e) for e in reponse.etapes]
        except Exception as exc:  # serveur MCP éteint, clé API invalide, quota dépassé…
            while isinstance(exc, ExceptionGroup):  # le client MCP emballe les erreurs
                exc = exc.exceptions[0]
            if "429" in str(exc):
                texte = "⚠️ Quota Mistral dépassé (ou modèle non inclus dans l'offre gratuite). Réessayez dans une minute ou changez MISTRAL_MODEL."
            elif "connect" in str(exc).lower():
                texte = f"⚠️ Serveur MCP injoignable sur `{agent.MCP_URL}`. Est-il lancé ?"
            else:
                texte = f"⚠️ Erreur : `{exc}`"
            etapes = []

    messages.append({"role": "assistant", "content": texte, "etapes": etapes, "mode": mode})
    conversation["date"] = datetime.now().isoformat()
    sauver_historique()
    st.rerun()  # réaffiche proprement : exemples masqués, historique à jour
