# POC — IA conversationnelle sur données bancaires (Mistral + MCP GCP)

Poser des questions en français sur une base bancaire **fictive** stockée dans BigQuery.

```
Streamlit ──► agent.py ──► Mistral (comprend, choisit l'outil, écrit le SQL, reformule)
                  │
                  └──► Serveur MCP « MCP Toolbox for Databases » (Google) ──► BigQuery
                         (lecture seule, dataset limité, plafond de coût)
```

## Ce qu'il y a dans le projet

| Fichier | Rôle |
|---|---|
| `data/generer_donnees.py` | Génère 600 clients, ~1 600 comptes, ~110 000 transactions, ~260 crédits (fictifs) |
| `data/charger_bigquery.py` | Crée le dataset `banque_demo` dans BigQuery et charge les CSV |
| `semantique/couche_semantique.yaml` | **Couche sémantique** : tables, définitions des indicateurs, synonymes, règles, exemples |
| `mcp/tools.bigquery.yaml` | Config du serveur MCP : source BigQuery + outils exposés à l'IA |
| `mcp/tools.sqlite.yaml` | Même chose sur une base SQLite locale (pour tester sans GCP) |
| `app/agent.py` | La boucle Mistral ⇄ outils MCP |
| `app/app.py` | L'interface de chat Streamlit |

### Où est la couche sémantique ?

À **deux** endroits, c'est voulu :

1. **`couche_semantique.yaml`**, injecté dans le prompt de Mistral : le dictionnaire métier
   (« encours », « taux d'impayés », « client à risque »…) et des exemples de requêtes validés.
2. **Les outils `kpi_*`, `fiche_client`, `depenses_par_categorie` du serveur MCP** : des
   indicateurs calculés en SQL figé, validé une fois. L'IA les appelle au lieu de réinventer le
   calcul, donc deux personnes qui posent la même question obtiennent le même chiffre.

Pour les questions imprévues, l'IA retombe sur `executer_sql` (SQL libre, en lecture seule).

## Installation (une fois)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python data/generer_donnees.py   # crée data/csv/ et data/banque.db (données fictives)
```

Télécharger le serveur MCP Toolbox (macOS Apple Silicon ; autres OS : voir
https://github.com/googleapis/genai-toolbox/releases) :

```bash
mkdir -p bin && curl -L -o bin/toolbox https://storage.googleapis.com/mcp-toolbox-for-databases/v1.15.0/darwin/arm64/toolbox && chmod +x bin/toolbox
```

Clé Mistral gratuite : https://console.mistral.ai → offre « Experiment » → *API Keys*.
Sur l'offre gratuite, `mistral-small` / `mistral-medium` peuvent être bloqués (erreur 429,
limite à 0 req/min) : utiliser `ministral-14b-latest` (défaut du projet), qui marche très bien ici.

```bash
cp .env.example .env    # puis remplir MISTRAL_API_KEY et GCP_PROJECT
```

## Option A : test rapide en local (sans GCP)

Mettre `DIALECTE=sqlite` dans `.env`, puis dans **deux terminaux** :

```bash
./bin/toolbox --config mcp/tools.sqlite.yaml
```

```bash
source .venv/bin/activate && streamlit run app/app.py
```

## Option B : avec BigQuery (le vrai test « MCP GCP »)

1. **Projet GCP gratuit** : le *bac à sable BigQuery* (sans carte bancaire) suffit :
   10 Go de stockage, 1 To de requêtes par mois. Les tables y expirent au bout de 60 jours ;
   relancer le chargement si besoin.
2. **Authentification** (ton compte Google, en local) :
   ```bash
   gcloud auth application-default login
   ```
3. **Charger les données** (`DIALECTE=bigquery` dans `.env`) :
   ```bash
   python data/charger_bigquery.py
   ```
4. **Lancer le serveur MCP**, qui lit `GCP_PROJECT` dans l'environnement :
   ```bash
   set -a && source .env && set +a && ./bin/toolbox --config mcp/tools.bigquery.yaml
   ```
5. **Lancer l'app** dans un autre terminal :
   ```bash
   source .venv/bin/activate && streamlit run app/app.py
   ```

Dans l'app, ouvrir **« 🔧 Ce que l'IA a fait »** sous chaque réponse : on y voit quel outil MCP a
été appelé, le SQL généré et les données brutes renvoyées.

### Bonus : voir que MCP est réutilisable

Le même serveur peut être branché tel quel dans un autre client MCP (Claude Desktop, Cursor,
VS Code, ou Le Chat de Mistral via un tunnel), sans réécrire une ligne de code. C'est l'intérêt
de MCP par rapport à un pipeline codé en dur. Pour explorer les outils à la main :

```bash
npx @modelcontextprotocol/inspector
```

Puis se connecter en « Streamable HTTP » à `http://127.0.0.1:5000/mcp/banque`.

## Option C : serveur MCP distant de Google (rien à héberger)

Google héberge son propre serveur MCP BigQuery (`https://bigquery.googleapis.com/mcp`). Il est
activé automatiquement avec l'API BigQuery. **Pas de Toolbox à lancer**, il suffit de :

```bash
gcloud auth application-default login
source .venv/bin/activate && streamlit run app/app.py
```

Puis choisir **« MCP distant Google (hébergé) »** dans la barre de gauche. Chaque réponse indique
quel serveur l'a produite, ce qui permet de comparer les deux modes sur les mêmes questions.

| | MCP local (Toolbox) | MCP distant Google |
|---|---|---|
| Hébergement | Un programme à faire tourner (poste, Cloud Run…) | Rien : service géré par Google |
| Outils | Sur mesure : `kpi_*`, `fiche_client`… + SQL libre | Génériques : `execute_sql_readonly`, `get_table_info`… |
| Couche sémantique | Dans les outils MCP **et** dans le prompt | Uniquement dans le prompt (les requêtes `kpi_*` y sont injectées comme « requêtes de référence ») |
| Lecture seule | `writeMode: blocked` dans la config | Outil `execute_sql_readonly` (SELECT uniquement) |
| Périmètre de données | `allowedDatasets` dans la config | Droits IAM du compte utilisé + filtre dans `agent.py` |
| Traçabilité | Journaux de Toolbox + historique BigQuery | Journaux d'audit Google Cloud centralisés |
| Droits nécessaires | Lecture BigQuery | `roles/mcp.toolUser` + `roles/bigquery.jobUser` + `roles/bigquery.dataViewer` |

En mode distant, l'app n'expose à Mistral que 3 outils en lecture seule (`list_table_ids`,
`get_table_info`, `execute_sql_readonly`) et impose elle-même le projet et le dataset.

**Coût :** contrairement à Toolbox, le serveur distant n'accepte pas de plafond par requête.
L'app fait donc d'abord une estimation gratuite (« dry run » BigQuery) et refuse toute requête
qui lirait plus de 100 Mo. En entreprise, compléter par des **quotas BigQuery** au niveau du projet
ou de l'utilisateur (quantité de données interrogées par jour).

En entreprise, **le vrai cloisonnement se fait par IAM** : un compte de service qui n'a accès
qu'aux datasets autorisés.

## Garde-fous en place

- `writeMode: blocked` côté BigQuery et `mode=ro` côté SQLite : le serveur refuse toute écriture.
- `allowedDatasets: [banque_demo]` : l'IA ne voit rien d'autre dans le projet.
- `maximumBytesBilled: 100 Mo` : une requête trop coûteuse est rejetée avant exécution.
- Double vérification dans `agent.py` : seuls les `SELECT` / `WITH` partent vers `executer_sql`.

## Limites à garder en tête pour le passage en entreprise

- **Offre gratuite Mistral** : débit limité, et les données envoyées peuvent servir à
  l'entraînement. Pour de vraies données : offre payante, Mistral déployé dans le cloud de
  l'entreprise (Vertex AI propose les modèles Mistral) ou modèle auto-hébergé.
- Les résultats d'outils (donc des données) sont envoyés au LLM : à valider avec la conformité / le DPO.
- En production : un compte de service dédié en lecture seule (`roles/bigquery.dataViewer` et
  `roles/bigquery.jobUser`) plutôt que ton compte perso.
- La qualité dépend surtout de la couche sémantique : l'enrichir avec les vraies définitions métier
  et des exemples validés.
