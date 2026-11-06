# Snowballing : extraction des bibliographies avec GROBID

Outil pour l'étape de backward snowballing d'une revue systématique de la littérature (SLR). À partir d'un dossier de PDF (les études incluses), il extrait la bibliographie de chaque article avec GROBID, regroupe les références citées par plusieurs articles et prépare les fichiers de screening.

Version 3.3.3 (30/09/2026). Auteure : Aurélie Jeunejean. Licence MIT (voir `LICENSE`).

## Principe

![Schéma de principe du script](images/figure%201.jpg)

## Contenu

| Fichier | Rôle |
|---|---|
| `extract_bibliography.py` | Le script (Python 3.8 ou plus récent) |
| `requirements.txt` | Paquets Python nécessaires (pypdf), installés automatiquement par les lanceurs |
| `LICENSE` | Licence MIT |
| `README.md` | Ce mode d'emploi |
| `images/` | Schéma de principe (`figure 1.jpg`) et sa source draw.io (`figures.drawio`) |

## Ce qu'il faut avant de commencer

1. **Un serveur GROBID** qui répond. Par défaut, le script utilise `http://localhost:8070` (GROBID installé sur son propre ordinateur, voir la documentation du projet ou https://grobid.readthedocs.io). Un autre serveur s'indique avec `--grobid-url`. Ne pas utiliser la démonstration publique de GROBID : elle est limitée et on y enverrait des articles protégés par le droit d'auteur.
2. **Python 3.8 ou plus récent et le paquet `pypdf`.** Les lanceurs cherchent Python et proposent de l'installer s'il est absent. Au premier lancement, ils créent un environnement virtuel `.venv` dans le dossier de l'outil et y installent les paquets de `requirements.txt` (connexion internet nécessaire la première fois). Rien n'est installé dans le Python du système. Pour réinstaller les paquets, supprimer le dossier `.venv`.
3. **Les PDF**, nommés selon la convention ci-dessous.
4. Facultatif : **les exports Covidence** (Export, bloc References, format CSV) des études incluses et des études écartées.

## Installation

1. Télécharger le zip et le décompresser.
   - Sous **Windows**, avant de décompresser : clic droit sur le zip, Propriétés, cocher « Débloquer », OK. Puis clic droit, « Extraire tout ». Ne jamais lancer le `.bat` depuis l'intérieur du zip.
2. Placer le contenu dans le dossier de travail de la SLR, à côté d'un dossier `PDF` :

```
MA_SLR/
    extract_bibliography.py
    PDF/        les articles
    output/     créé par le script : tous les fichiers produits
```

Dans le dossier de l'outil :

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python extract_bibliography.py
```

Sous Windows, remplacer `.venv/bin/python` par `.venv\Scripts\python.exe`. Ne pas faire `pip install` en dehors de `.venv` : sur macOS avec Homebrew, c'est refusé (`externally-managed-environment`).

## Convention de nommage des PDF

```
<n° Covidence sur 3 chiffres>_<année>_<nom du premier auteur>_<titre>.pdf
001_2024_Omer_Development of a 3D Digital Model of End-of-Service-Life Buildings.pdf
```

Nom de famille du premier auteur uniquement, sans « et al », un seul tiret bas entre les éléments, pas de caractères problématiques dans le titre (deux-points, barre oblique, point d'interrogation). Avec `--included`, le script vérifie les noms et propose le nom corrigé. Renommer un PDF est sans risque : le script le reconnaît grâce à son empreinte et ne le retraite pas.

## Diagnostic des PDF et fichiers .refs.txt

Avant d'envoyer un PDF à GROBID, le script le lit avec `pypdf` : nombre de pages, caractères par page, présence d'un titre de bibliographie (« Références », « References », « Bibliographie »...). Deux cas sont signalés :

- **PDF sans texte exploitable** (moins de 300 caractères par page : PDF scanné, « imprimé en PDF », ou polices sans correspondance Unicode) : il n'est pas envoyé à GROBID (statut `no_text`, contrôle `PDF_NO_TEXT`).
- **Bibliographie présente mais non reconnue par GROBID**, fréquent pour les posters et les articles très courts (contrôle `BIBLIOGRAPHY_NOT_RECOGNISED`).

Dans les deux cas, la solution est un **fichier `.refs.txt`** placé à côté du PDF, avec le même nom :

```
PDF/2026_Hofmann_From Inventory to Reuse.pdf
PDF/2026_Hofmann_From Inventory to Reuse.refs.txt
```

1. Copier les références dans ce fichier texte : une référence par ligne, ou, si une référence tient sur plusieurs lignes, séparer les références par une ligne vide. Les lignes commençant par `#` sont ignorées.
2. Relancer le script : GROBID structure ces références et les complète avec Crossref (`origin` = `citation_list`). Modifier le fichier plus tard relance automatiquement son traitement.

**Pour un PDF sans texte**, copier depuis le PDF ne donne rien : faire lire le texte d'une capture d'écran de la bibliographie par le système.

- macOS (12 ou plus récent) : Cmd+Maj+4 pour capturer la bibliographie, ouvrir l'image dans Aperçu, sélectionner le texte, copier.
- Windows 11 : Outil Capture d'écran, puis « Actions de texte », puis « Copier tout le texte ».

Relire ensuite les accents, les années et les DOI avant d'enregistrer le fichier. Mieux encore, quand c'est possible : récupérer le PDF original (éditeur, dépôt institutionnel, PDF produit par LaTeX) et remplacer le PDF défectueux.

## Utilisation

Double-cliquer sur le lanceur, ou en ligne de commande depuis le dossier de travail :

```
python3 extract_bibliography.py
python3 extract_bibliography.py --included covidence_included.csv --screened covidence_excluded.csv --flag-grey
```

Principales options (liste complète : `python3 extract_bibliography.py --help`) :

| Option | Rôle | Défaut |
|---|---|---|
| `--pdf-dir` | Dossier des PDF | `./PDF` |
| `--output-dir` | Dossier des fichiers produits | `./output` |
| `--grobid-url` | Serveur GROBID | `http://localhost:8070` |
| `--included` | Export Covidence des études incluses : contrôle des noms, références déjà dans le corpus | aucun |
| `--screened` | Autres exports Covidence : références déjà screenées | aucun |
| `--flag-grey` | Pré-sélection de la littérature grise + échantillon de validation de 10 % | désactivé |
| `--reprocess-changed` | Réextraire les PDF modifiés | désactivé |
| `--prune-missing` | Supprimer les lignes des PDF retirés du dossier | désactivé |
| `--retry-empty` | Renvoyer les PDF sans référence | désactivé |

À chaque exécution, la console affiche un bilan et les étapes suivantes.

## Fichiers produits (dans `output/`)

| Fichier | Que faire ? |
|---|---|
| `bibliography_README.txt` | Le lire en premier : il explique chaque fichier |
| `bibliography.csv` | Fichier de travail : une ligne par référence. Corrections manuelles ici, puis relancer |
| `bibliography_unique.csv` | Candidats au screening. `cited_by_count` sert à ordonner la lecture, jamais de seuil |
| `bibliography_checks.csv` | Cas à vérifier : remplir `verified` et `decision` (conservés d'une exécution à l'autre) |
| `bibliography_grey_validation.csv` | Avec `--flag-grey` : remplir `reviewer_is_grey` (yes / no) |
| `bibliography_manifest.csv` | État de chaque PDF (ok, failed, no_refs, manual...) |
| `run_log.json` | Historique des exécutions : version de GROBID et réglages, pour la méthodologie |
| `backups/` | Copies de `bibliography.csv` avant chaque exécution |

Ne jamais réenregistrer `bibliography.csv` depuis Excel sans vérifier la colonne `pages` : Excel transforme « 6-9 » en date.

## Corrections manuelles dans bibliography.csv

| Problème | Correction |
|---|---|
| DOI faux | Corriger la colonne `doi` |
| Deux références différentes fusionnées (même DOI) | Valeur unique dans `dedup_override` sur l'une des lignes, par ex. `manual:ding2018b` |
| Même référence non fusionnée | Même valeur dans `dedup_override` sur toutes les lignes, par ex. `manual:akanbi2020` |
| Références saisies à la main dans `bibliography.csv` | `origin` = `manual` : jamais modifiées par le script |

Relancer le script après chaque correction : tous les fichiers sont recalculés.

## Adapter l'outil à une nouvelle SLR

Les éléments propres à une revue se trouvent en tête du script, dans des blocs commentés :

- `GREY_DOI_PREFIXES` et `GREY_RULES` : règles de littérature grise, écrites pour la construction circulaire. À relire pour chaque protocole, puis à valider avec l'échantillon de 10 %.
- `MIN_TITLE_OVERLAP`, `MAX_RAW_CITATION_LENGTH`, `MIN_FILENAME_TITLE_OVERLAP` : seuils des contrôles qualité.
- `--min-refs` : nombre minimal de références attendu par article.

## Dépannage

| Message | Cause et solution |
|---|---|
| `GROBID does not answer` | Le serveur n'est pas démarré ou l'adresse est fausse. Rien n'a été envoyé : démarrer GROBID ou passer `--grobid-url`, puis relancer |
| `The Python package 'pypdf' is missing` | Utiliser le lanceur, ou installer les paquets dans `.venv` (voir plus haut) |
| `not sent to GROBID: no usable text layer` | PDF sans texte exploitable : PDF original, ou fichier `.refs.txt` (voir plus haut) |
| `a bibliography is present but GROBID did not recognise it` | Poster ou article court : fichier `.refs.txt` |
| `Cannot write ... open in Excel` | Fermer le fichier dans Excel et relancer : rien n'est perdu |
| Les colonnes `verified` / `decision` se vident après un lancement | Versions antérieures à 3.3.2 : le CSV avait été réenregistré avec des points-virgules (Excel en français). Mettre à jour le script ; enregistrer de préférence en « CSV UTF-8 (délimité par des virgules) » |
| `column(s) ... not found` | Le CSV a été réenregistré dans un format illisible : l'enregistrer en « CSV UTF-8 » ou le restaurer depuis `output/backups` |
| `PDF folder not found` | Lancer le script depuis le dossier de travail, utiliser le lanceur, ou passer `--pdf-dir` |
| Un PDF n'est pas détecté | Vérifier qu'il est bien dans `PDF/` avec l'extension `.pdf` |

## Passation : séance de prise en main

Liste à parcourir ensemble lors de la première utilisation par une nouvelle chercheuse :

- [ ] Décompresser le zip, débloquer les fichiers et lancer le lanceur une première fois
- [ ] Vérifier que Python est trouvé (version affichée par le lanceur)
- [ ] Vérifier l'accès à un serveur GROBID (`--grobid-url` si ce n'est pas `localhost`)
- [ ] Nommer trois PDF selon la convention et lancer une extraction de test
- [ ] Lire le bilan de la console et `output/bibliography_README.txt`
- [ ] Corriger un cas de `bibliography_checks.csv` avec `dedup_override`, relancer, constater le résultat
- [ ] Exporter les études incluses depuis Covidence et relancer avec `--included`
- [ ] Relire les règles de littérature grise au regard de son propre protocole
- [ ] Savoir où trouver `run_log.json` pour rédiger la méthodologie
- [ ] Savoir restaurer une sauvegarde depuis `output/backups/`

## Citer l'outil

> Jeunejean, A. (2026). *extract_bibliography.py : extraction des bibliographies avec GROBID pour le snowballing en revue systématique* (version 3.3.3) [Logiciel].

Et citer GROBID : Lopez, P. (2009). GROBID: Combining automatic bibliographic data recognition and term extraction for scholarship publications. In *Research and Advanced Technology for Digital Libraries (ECDL 2009)*, LNCS 5714, pp. 473-474. https://doi.org/10.1007/978-3-642-04346-8_62
