# Proposition de provider Codex pour multiagents

Ce répertoire contient une intégration proposée, **non installée et non activée**.
Aucun fichier existant du projet n'est modifié. Il s'agit d'un provider du moteur
Python `multiagents`, pas d'un plugin à installer dans Codex : le nom du répertoire
est celui demandé, et aucun manifeste `.codex-plugin/plugin.json` n'est nécessaire.

## Fichiers livrés

| Fichier | Rôle |
| --- | --- |
| `providers.yaml` | Provider déclaratif, arguments, permissions, reprise, modèles, MCP, profils et règles du flux |
| `providers/codex.py` | Adaptateur exécutable Python 3.11+, sans dépendance externe, utilisé comme binaire et script d'actions |
| `examples/agents.yaml` | Exemples de lecteur, développeur et orchestrateur à adapter |
| `examples/project-docker.yaml` | Ajouts Docker proposés, chemins à renseigner |
| `tests/test_codex.py` | Tests hors ligne avec le vrai `Provider` du dépôt et un faux CLI |

## Analyse du projet et choix d'architecture

Le contrat est défini dans `src/multiagents/providers.py` et
`src/multiagents/defaults/providers/README.md`. `scripts.py` résout les actions
projet → global → défauts ; `runner.py` gère le HOME privé, l'identité des enfants,
les worktrees, le superviseur, les budgets et la reprise. `driver.py` lance les
rôles interactifs. `executor/docker.py` monte le binaire déclaré et les profils.

Deux contraintes empêchent une intégration complète avec seulement du YAML :

1. `Provider.build_command()` ajoute les arguments de reprise après ceux du
   lancement initial ; Codex utilise une sous-commande `exec resume` avec une
   grammaire différente. L'adaptateur construit cette commande explicitement.
2. `Runner._hand_server()` sérialise le MCP en JSON. L'adaptateur lit ce JSON et
   transmet une table TOML via les options `-c` de Codex, sans modifier sa
   configuration globale.

Une troisième adaptation concerne les tokens : l'entrée Codex inclut les tokens
en cache ; multiagents additionne des catégories disjointes. L'adaptateur expose
`input_tokens = input_tokens_codex - cached_input_tokens`, la lecture du cache
séparément et `total_tokens = entrée_codex + sortie`. Aucun tarif n'est inventé.

## Couverture du contrat

| Fonctionnalité | Proposition et limites |
| --- | --- |
| Disponibilité | `bin: multiagents-codex` doit être sur PATH ; `check` vérifie aussi le CLI natif |
| Exécution | `codex exec --json`, prompt transmis par stdin, cwd explicite, sans shell |
| Modèle / effort | Modèle du roster, `effort` traduit en `model_reasoning_effort` pour les agents exécutés |
| Permissions | `readonly` → `read-only`, `sandbox` → `workspace-write`, `full` → `danger-full-access` ; approbations non interactives `never` |
| Reprise d'un enfant | UUID reçu dans `thread.started`, puis `codex exec resume UUID` |
| Texte | Messages terminés seulement, pour éviter les doublons des mises à jour |
| Outils / détection de boucle | Commandes, modifications, recherche et MCP ; un événement outil par identifiant, arguments réels et identifiant de tour |
| Progression | Événements intermédiaires classés `step`, types inconnus conservés `raw` avec la charge Codex |
| Résultat / erreurs | Succès explicite, échec de tour, code de sortie non nul et absence de résultat terminal distingués |
| Quota / authentification en erreur | Message d'échec également sur stderr pour les détecteurs existants du moteur |
| Tokens | Deltas par `turn.completed`, cache séparé sans double comptage |
| Coût monétaire | Non fourni par le flux ; les zéros éventuellement affichés par le moteur ne signifient pas gratuité |
| `check` | `codex login status`, timeout 15 s ; codes 0 / 10 / 20 ; pas de génération ni affichage du contenu d'authentification |
| `login` | Connexion interactive par code appareil, avec annonce du profil cible |
| `budget` | JSON `known: false` ; aucun appel à une API privée ni estimation du quota à partir des tokens |
| `usage` | Trois lignes locales, lit `MULTIAGENTS_BUDGET`, aucun nouveau sondage |
| `prepare` | Validation du fichier MCP, idempotente, sans enregistrement global |
| `launch` | Orchestrateur / initializer interactifs, brief, modèle, consigne de reprise et MCP ; mode unattended via `exec` |
| `compact` | Code 64, y compris en mode probe ; aucune fausse commande `/compact` envoyée au modèle |
| Modèles | Lecture optionnelle du cache local `models_cache.json`, sortie TSV ; liste statique configurable si absent |
| Isolation locale | HOME privé du moteur avec lien vers le répertoire `.codex` du compte |
| Docker | Profil `.codex` privé et login dans son backing host ; binaire natif et réseau à configurer |
| Comptes multiples | `extends`, `family` et `CODEX_HOME` du moteur réutilisables ; voir restrictions ci-dessous |
| Arrêt / timeout | Groupe de processus géré par le moteur, SIGINT/SIGTERM relayés au CLI ; aucun timeout fournisseur concurrent |

Les serveurs MCP hérités sont recensés localement par `codex mcp list --json`,
puis désactivés explicitement par invocation. Une table vide ne suffit pas :
Codex fusionne les réglages, comportement vérifié avec le CLI installé. Seul le
serveur transmis est réactivé ; un échec du recensement bloque le lancement.
Le serveur multiagents est requis lorsqu'il
est présent ; un agent marqué `MULTIAGENTS_CAN_SPAWN=1` sans sa configuration est
refusé. Les sous-agents natifs Codex sont désactivés via `features.multi_agent=false`
pour que les délégations passent par l'identité et les limites de multiagents.
Cela ne transforme pas le sandbox shell en contrôle des effets des outils MCP :
un agent lecteur devrait rester `can_spawn: false` comme dans l'exemple.

## Intégration ultérieure — commandes non exécutées

Après revue, depuis la racine du projet, copier le script et créer son alias :

```sh
mkdir -p .multiagents/config/providers "$HOME/.local/bin"
install -m 755 codex-plugin/providers/codex.py .multiagents/config/providers/codex.py
ln -s "$PWD/.multiagents/config/providers/codex.py" "$HOME/.local/bin/multiagents-codex"
```

Si cet alias existe déjà, vérifier sa cible avant de le remplacer. Le répertoire
`~/.local/bin` doit être sur le PATH de multiagents, y compris dans le conteneur.
Pour plusieurs projets, préférer une copie globale sous
`~/.config/multiagents/providers/codex.py` et un alias vers cette copie.

Fusionner ensuite la clé `providers.codex` de `providers.yaml` dans
`.multiagents/config/providers.yaml`, sans écraser les autres providers. Fusionner
les exemples d'agents après remplacement des identifiants de modèles. Le script
natif attendu est `codex` sur PATH ; pour un chemin spécifique, renseigner
`providers.codex.env.MULTIAGENTS_CODEX_BIN` avec un chemin absolu.

```sh
multiagents auth login codex
multiagents refresh-models
multiagents probe codex
```

Ces commandes sont proposées seulement : login change le profil, refresh écrit
le catalogue et probe peut consommer des tokens. Aucune n'a été exécutée ici.
La détection des modèles via cache est indicative : le cache peut manquer ou
vieillir. Dans ce cas, ajouter une liste explicite sous le provider :

```yaml
models:
  - id: IDENTIFIANT_VALIDE_POUR_VOTRE_COMPTE
    label: Votre modèle Codex
```

Le moteur privilégie cette liste. Aucun identifiant de modèle disponible pour
votre abonnement n'est présumé. `refresh_models()` n'applique actuellement pas
`provider.env` à `models_cmd` : pour un compte secondaire, utiliser sa propre
liste statique, plutôt que prétendre avoir découvert ses droits.

## Authentification, comptes et Docker

Le stockage `cli_auth_credentials_store="file"` est imposé à chaque invocation,
pour utiliser le même profil dans le contrôle, le login et l'exécution. Une
connexion uniquement dans le trousseau système doit donc être refaite dans ce
profil. Une connexion par clé API peut être préparée manuellement avec
`codex -c 'cli_auth_credentials_store="file"' login --with-api-key` en alimentant
stdin ; le script `login` fourni choisit la connexion ChatGPT par appareil.

Le lien porte sur **le répertoire**, pas sur `auth.json`, afin qu'un renouvellement
atomique du fichier reste visible. En contrepartie, les agents du même compte
voient ses sessions et son état Codex. Ce n'est pas une isolation entre agents
du même provider. Docker utilise un profil privé distinct du profil hôte ; les
actions d'authentification choisissent `MULTIAGENTS_PRIVATE_BACKING`, sauf demande
explicite `MULTIAGENTS_PROFILE=host`. Le lancement de l'orchestrateur reste hôte.
Le proxy d'authentification spécifique à Claude n'est pas étendu à Codex.

Le montage automatique ne découvre que `multiagents-codex`. Il faut donc monter
ou installer aussi le CLI **natif** et ses dépendances : un lanceur npm nécessite
Node et son paquet complet. Le fragment Docker propose des chemins à remplacer
et les domaines à ajouter à la liste existante. Ne pas remplacer les listes
actuelles, car cela supprimerait les montages et domaines des autres providers.
La compatibilité du binaire, le réseau et le login Docker restent à vérifier en
conditions réelles ; aucune image ni aucun conteneur n'a été modifié ici.

Pour un second compte local :

```yaml
providers:
  codex-b:
    extends: codex
    family: codex
    env:
      CODEX_HOME: ~/.multiagents/profiles/codex-b
    models:
      - id: IDENTIFIANT_VALIDE_POUR_CE_COMPTE
        label: Modèle du compte B
```

En Docker, ce bloc ne suffit pas : un chemin privé distinct, son montage et le
`CODEX_HOME` vu dans le conteneur doivent désigner le même profil. Ne pas simplement
hériter `.codex` pour deux comptes : le moteur rejette les collisions de profils.
La famille permet le routage entre comptes, mais un quota inconnu ne permet pas
de répartir préventivement la charge selon la consommation réelle.

## Limites à connaître avant activation

- Le CLI ciblé est **0.157.1**, version observée localement. Les aides de `exec`,
  `exec resume` et `resume` ont été consultées. Une version ancienne peut ne pas
  comprendre `--ignore-user-config` ou les paramètres utilisés.
- Le lancement non interactif ignore la configuration utilisateur pour garder
  un contrat reproductible. Les providers API personnalisés, hooks, profils et
  réglages personnels ne sont pas automatiquement portés. Les politiques
  administrées de Codex restent prioritaires.
- `driver._launch_context()` ne transmet actuellement ni l'effort ni la
  permission du rôle. Le lancement interactif conserve la politique Codex ;
  unattended utilise explicitement workspace-write, sans approbation. L'effort
  du roster est transmis seulement aux agents exécutés par `Runner`.
- La reprise interactive mémorise un UUID par rôle dans le répertoire de launch.
  Après une première sortie normale, un unique nouveau transcript du projet est
  associé au rôle. En cas d'ambiguïté ou d'arrêt brutal avant cette association,
  le prochain lancement repart à neuf : jamais de `resume --last` sur une session
  étrangère. Unattended mémorise l'UUID dès l'événement `thread.started`.
- L'UUID synthétique de rôle attribué par le driver ne devient pas l'UUID natif
  Codex. Le watchdog et `session_context()` attendent des transcripts d'une autre
  forme : le provider ne déclare donc pas de faux `transcript`. Suivi de contexte
  du rôle, auto-compaction pilotée par le driver et attribution MCP des coûts
  ne sont pas disponibles. Leur prise en charge complète demanderait une
  extension du moteur ou un backend app-server, hors de cette proposition.
- La compaction native automatique de Codex reste sous son contrôle. Le code 64
  indique honnêtement l'absence d'une compaction externe vérifiée.
- Une longue commande peut ne produire aucun événement avant sa fin. Ajuster
  `silence_timeout` aux tâches ; l'adaptateur ne fabrique pas de progression.
- Les quotas restent inconnus. Les messages d'échec alimentent les détecteurs
  existants ; tous les nouveaux libellés futurs de Codex ne sont pas garantis.

## Validation

Depuis la racine du dépôt, avec l'environnement existant :

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider codex-plugin/tests
```

Les tests vérifient la construction réelle du provider, les trois permissions,
le prompt littéral, la reprise, les tokens, les outils sans doublons, MCP et ses
identités, les erreurs, les actions, les profils, le cache des modèles et la
sélection des sessions. Ils n'appellent aucun modèle, ne lisent aucune clé réelle
et ne modifient pas les fichiers existants du dépôt. Le login réel, l'interface
interactive, la délégation effective et Docker ne sont pas validés de bout en bout.

Résultat obtenu : **21 tests réussis**. Une vérification supplémentaire avec le
vrai Codex 0.157.1, dans un HOME temporaire sans authentification, confirme que
les serveurs hérités sont désactivés et que seul multiagents est réactivé quand
sa configuration est fournie. Aucun serveur MCP ni modèle n'a été lancé par
cette vérification (`mcp list --json` uniquement).

## Références officielles consultées

- [Exécution non interactive](https://developers.openai.com/codex/noninteractive) :
  flux JSONL, événements, reprise et contrôles d'exécution.
- [Référence CLI](https://developers.openai.com/codex/cli/reference) : commandes
  et options ; complétée par les aides de la version installée.
- [Configuration MCP](https://developers.openai.com/codex/mcp) : table de serveurs,
  commande stdio, arguments, environnement et serveur requis.
- [Authentification](https://developers.openai.com/codex/auth) : connexion,
  stockage fichier, `CODEX_HOME` et connexion par appareil.

Les formats du cache des modèles et des transcripts sont des détails internes,
donc traités comme des aides optionnelles, pas comme des API publiques stables.
