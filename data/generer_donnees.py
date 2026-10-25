"""Génère un jeu de données bancaires 100 % fictif.

Sorties :
  data/csv/*.csv      -> à charger dans BigQuery (charger_bigquery.py)
  data/banque.db      -> base SQLite pour tester en local sans GCP

Usage : python data/generer_donnees.py
"""

import csv
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

random.seed(42)  # mêmes données à chaque exécution

ICI = Path(__file__).parent
DOSSIER_CSV = ICI / "csv"
FICHIER_SQLITE = ICI / "banque.db"

DATE_REF = date(2026, 9, 30)  # "aujourd'hui" dans le jeu de données
NB_CLIENTS = 600

PRENOMS = ["Camille", "Léa", "Manon", "Chloé", "Inès", "Sarah", "Julie", "Emma", "Louise", "Alice",
           "Lucas", "Hugo", "Thomas", "Nathan", "Louis", "Gabriel", "Arthur", "Jules", "Paul", "Adam",
           "Nicolas", "Sophie", "Pierre", "Marie", "Antoine", "Claire", "Mehdi", "Yasmine", "Karim", "Nadia"]
NOMS = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau",
        "Simon", "Laurent", "Lefebvre", "Michel", "Garcia", "David", "Bertrand", "Roux", "Vincent", "Fournier",
        "Morel", "Girard", "André", "Mercier", "Dupont", "Lambert", "Bonnet", "François", "Martinez", "Benali"]

AGENCES = [
    ("AG01", "Paris Opéra", "Paris", "Île-de-France"),
    ("AG02", "Paris Bastille", "Paris", "Île-de-France"),
    ("AG03", "Lyon Part-Dieu", "Lyon", "Auvergne-Rhône-Alpes"),
    ("AG04", "Grenoble Centre", "Grenoble", "Auvergne-Rhône-Alpes"),
    ("AG05", "Marseille Vieux-Port", "Marseille", "Provence-Alpes-Côte d'Azur"),
    ("AG06", "Nice Masséna", "Nice", "Provence-Alpes-Côte d'Azur"),
    ("AG07", "Bordeaux Chartrons", "Bordeaux", "Nouvelle-Aquitaine"),
    ("AG08", "Lille Grand-Place", "Lille", "Hauts-de-France"),
]

SEGMENTS = [("Particulier", 0.70), ("Premium", 0.15), ("Professionnel", 0.15)]

# type de compte -> (probabilité de détention, solde moyen)
PRODUITS_EPARGNE = {
    "Livret A": (0.65, 6000),
    "LDDS": (0.35, 4000),
    "PEL": (0.20, 15000),
    "Assurance vie": (0.25, 40000),
    "Compte titres": (0.10, 25000),
}

DEPENSES = [  # (type_operation, categorie, libellés possibles, montant moyen)
    ("CB", "Alimentation", ["CARREFOUR", "MONOPRIX", "LIDL", "BIOCOOP", "BOULANGERIE"], 45),
    ("CB", "Transport", ["SNCF", "TOTALENERGIES", "UBER", "RATP NAVIGO"], 40),
    ("CB", "Loisirs", ["FNAC", "CINEMA PATHE", "DECATHLON", "RESTAURANT"], 55),
    ("CB", "Santé", ["PHARMACIE", "DOCTOLIB MEDECIN"], 30),
    ("Prélèvement", "Logement", ["LOYER", "EDF", "ENGIE", "ORANGE BOX"], 350),
    ("Prélèvement", "Assurances", ["AXA ASSURANCE", "MAIF", "MUTUELLE"], 80),
    ("Retrait DAB", "Espèces", ["RETRAIT DAB"], 60),
]


def date_aleatoire(debut: date, fin: date) -> date:
    return debut + timedelta(days=random.randint(0, (fin - debut).days))


def arrondi(x: float) -> float:
    return round(x, 2)


def generer():
    agences = [dict(agence_id=a, nom_agence=n, ville=v, region=r) for a, n, v, r in AGENCES]

    clients, comptes, transactions, credits = [], [], [], []
    num_compte = num_tx = num_credit = 0

    for i in range(1, NB_CLIENTS + 1):
        segment = random.choices([s for s, _ in SEGMENTS], [p for _, p in SEGMENTS])[0]
        agence = random.choice(AGENCES)
        client_id = f"CL{i:05d}"
        date_entree = date_aleatoire(date(2005, 1, 1), date(2026, 6, 30))
        clients.append(dict(
            client_id=client_id,
            prenom=random.choice(PRENOMS),
            nom=random.choice(NOMS),
            date_naissance=date_aleatoire(date(1945, 1, 1), date(2005, 12, 31)).isoformat(),
            ville=agence[2],
            segment=segment,
            agence_id=agence[0],
            date_entree=date_entree.isoformat(),
            score_risque=random.choices(range(1, 11), [5, 9, 12, 14, 14, 13, 11, 9, 7, 6])[0],
        ))

        multiplicateur = {"Particulier": 1, "Premium": 4, "Professionnel": 2.5}[segment]

        # Compte courant (tout le monde en a un)
        num_compte += 1
        cc_id = f"CP{num_compte:06d}"
        clos = random.random() < 0.05
        comptes.append(dict(
            compte_id=cc_id, client_id=client_id, type_compte="Compte courant",
            date_ouverture=date_entree.isoformat(), statut="clos" if clos else "actif",
            solde=0.0 if clos else arrondi(random.gauss(2500, 2000) * multiplicateur),
        ))

        # Produits d'épargne
        for type_compte, (proba, solde_moyen) in PRODUITS_EPARGNE.items():
            if random.random() < proba * (1.5 if segment == "Premium" else 1):
                num_compte += 1
                comptes.append(dict(
                    compte_id=f"CP{num_compte:06d}", client_id=client_id, type_compte=type_compte,
                    date_ouverture=date_aleatoire(date_entree, DATE_REF).isoformat(), statut="actif",
                    solde=arrondi(abs(random.gauss(solde_moyen, solde_moyen * 0.6)) * multiplicateur),
                ))

        # Transactions sur 12 mois (compte courant actif uniquement)
        if not clos:
            salaire = arrondi(random.gauss(2600, 700) * multiplicateur)
            for mois in range(12):
                index = DATE_REF.year * 12 + DATE_REF.month - 1 - (11 - mois)
                debut_mois = date(index // 12, index % 12 + 1, 1)
                lignes = [("Virement", "Revenus", "SALAIRE" if segment != "Professionnel" else "ENCAISSEMENT CLIENT",
                           salaire * random.uniform(0.9, 1.1), 1)]
                for type_op, cat, libelles, moyenne in DEPENSES:
                    nb = random.randint(0, 6) if type_op == "CB" else random.randint(0, 2)
                    for _ in range(nb):
                        lignes.append((type_op, cat, random.choice(libelles),
                                       -abs(random.gauss(moyenne, moyenne * 0.5)) * multiplicateur ** 0.5, None))
                if random.random() < 0.3:
                    lignes.append(("Virement", "Épargne", "VIREMENT VERS LIVRET", -random.choice([100, 200, 500]), None))
                for type_op, cat, libelle, montant, jour in lignes:
                    num_tx += 1
                    jour = jour or random.randint(1, 28)
                    transactions.append(dict(
                        transaction_id=f"TX{num_tx:07d}", compte_id=cc_id,
                        date_operation=debut_mois.replace(day=jour).isoformat(),
                        montant=arrondi(montant), type_operation=type_op, categorie=cat, libelle=libelle,
                    ))

        # Crédits
        proba_credit = {"Particulier": 0.35, "Premium": 0.5, "Professionnel": 0.6}[segment]
        if random.random() < proba_credit:
            num_credit += 1
            type_credit = random.choices(
                ["Immobilier", "Consommation", "Auto", "Professionnel"],
                [5, 3, 2, 6 if segment == "Professionnel" else 0])[0]
            plage_montant, plage_duree, taux_base = {
                "Immobilier": ((80000, 450000), (180, 300), 3.2),
                "Consommation": ((2000, 25000), (12, 72), 5.8),
                "Auto": ((8000, 45000), (24, 72), 4.9),
                "Professionnel": ((20000, 300000), (36, 120), 4.5),
            }[type_credit]
            montant = arrondi(random.uniform(*plage_montant))
            duree = random.randint(*plage_duree)
            date_debut = date_aleatoire(date(2015, 1, 1), date(2026, 8, 31))
            mois_ecoules = (DATE_REF.year - date_debut.year) * 12 + DATE_REF.month - date_debut.month
            if mois_ecoules >= duree:
                statut, restant = "soldé", 0.0
            else:
                restant = arrondi(montant * (1 - mois_ecoules / duree))
                # plus le score de risque est élevé, plus l'impayé est probable
                statut = "impayé" if random.random() < clients[-1]["score_risque"] * 0.012 else "en cours"
            credits.append(dict(
                credit_id=f"CR{num_credit:05d}", client_id=client_id, type_credit=type_credit,
                montant_initial=montant, taux_annuel=round(taux_base + random.uniform(-0.8, 1.2), 2),
                duree_mois=duree, date_debut=date_debut.isoformat(),
                capital_restant_du=restant, statut=statut,
            ))

    return dict(agences=agences, clients=clients, comptes=comptes,
                transactions=transactions, credits=credits)


def ecrire_csv(tables):
    DOSSIER_CSV.mkdir(exist_ok=True)
    for nom, lignes in tables.items():
        with open(DOSSIER_CSV / f"{nom}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=lignes[0].keys())
            w.writeheader()
            w.writerows(lignes)


def ecrire_sqlite(tables):
    FICHIER_SQLITE.unlink(missing_ok=True)
    con = sqlite3.connect(FICHIER_SQLITE)
    for nom, lignes in tables.items():
        colonnes = list(lignes[0].keys())
        def type_sql(v):
            return "INTEGER" if isinstance(v, int) else "REAL" if isinstance(v, float) else "TEXT"
        ddl = ", ".join(f"{c} {type_sql(lignes[0][c])}" for c in colonnes)
        con.execute(f"CREATE TABLE {nom} ({ddl})")
        con.executemany(f"INSERT INTO {nom} VALUES ({', '.join('?' * len(colonnes))})",
                        [tuple(l[c] for c in colonnes) for l in lignes])
    con.commit()
    con.close()


if __name__ == "__main__":
    tables = generer()
    ecrire_csv(tables)
    ecrire_sqlite(tables)
    for nom, lignes in tables.items():
        print(f"{nom:<13} {len(lignes):>7} lignes")
    print(f"\nCSV    -> {DOSSIER_CSV}\nSQLite -> {FICHIER_SQLITE}")
