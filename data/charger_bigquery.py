"""Charge les CSV fictifs dans BigQuery (dataset `banque_demo`).

Pré-requis :
  gcloud auth application-default login
  variable GCP_PROJECT dans le fichier .env (ou l'environnement)

Usage : python data/charger_bigquery.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv(Path(__file__).parent.parent / ".env")

PROJET = os.environ["GCP_PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "banque_demo")
LOCALISATION = os.environ.get("BQ_LOCATION", "EU")
DOSSIER_CSV = Path(__file__).parent / "csv"

S, F, I, D = "STRING", "FLOAT64", "INT64", "DATE"
SCHEMAS = {
    "agences": [("agence_id", S), ("nom_agence", S), ("ville", S), ("region", S)],
    "clients": [("client_id", S), ("prenom", S), ("nom", S), ("date_naissance", D), ("ville", S),
                ("segment", S), ("agence_id", S), ("date_entree", D), ("score_risque", I)],
    "comptes": [("compte_id", S), ("client_id", S), ("type_compte", S), ("date_ouverture", D),
                ("statut", S), ("solde", F)],
    "transactions": [("transaction_id", S), ("compte_id", S), ("date_operation", D), ("montant", F),
                     ("type_operation", S), ("categorie", S), ("libelle", S)],
    "credits": [("credit_id", S), ("client_id", S), ("type_credit", S), ("montant_initial", F),
                ("taux_annuel", F), ("duree_mois", I), ("date_debut", D),
                ("capital_restant_du", F), ("statut", S)],
}


def main():
    client = bigquery.Client(project=PROJET)
    dataset = bigquery.Dataset(f"{PROJET}.{DATASET}")
    dataset.location = LOCALISATION
    dataset.description = "Données bancaires FICTIVES pour POC IA conversationnelle"
    client.create_dataset(dataset, exists_ok=True)

    for table, colonnes in SCHEMAS.items():
        config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=1,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=[bigquery.SchemaField(nom, type_) for nom, type_ in colonnes],
        )
        with open(DOSSIER_CSV / f"{table}.csv", "rb") as f:
            job = client.load_table_from_file(f, f"{PROJET}.{DATASET}.{table}", job_config=config)
        job.result()
        print(f"{table:<13} {client.get_table(job.destination).num_rows:>7} lignes chargées")

    print(f"\nOK -> https://console.cloud.google.com/bigquery?project={PROJET}")


if __name__ == "__main__":
    main()
