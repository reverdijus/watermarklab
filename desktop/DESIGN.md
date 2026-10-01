# Watermark Lab — design system du desktop

Guide court pour écrire de nouvelles pages (DESKTOP-B et suivants). Tout est en
français côté UI ; code, identifiants et clés de métriques en anglais.

## 1. Principes

- **À la Claude** : calme, épuré, beaucoup d'espace, fond chaud, une seule couleur
  d'accent (argile) réservée aux actions primaires et à l'état actif.
- Titres de page et de section en **serif système** (`font-serif`), UI en
  `system-ui`, valeurs techniques (ids, clés, YAML, logs) en `font-mono`.
- Bordures fines (`border-border`), coins 10–14 px (`rounded-xl` pour les cartes,
  `rounded-[10px]` pour les contrôles), ombres très légères (`shadow-sm`).
- Aucune couleur criarde : statuts en teintes douces (`bg-success/12 text-success`…).
- Nombres en `tabular` (chiffres alignés) ; tout passe par `lib/format.ts`.

## 2. Tokens (src/index.css)

Définis sur `:root` (clair), surchargés par `prefers-color-scheme: dark` et forcés par
`<html data-theme="light|dark">`. Classes Tailwind correspondantes :

| token CSS | classe Tailwind | usage |
|---|---|---|
| `--bg` | `bg-canvas` | fond de page |
| `--sidebar` | `bg-sidebar` | barre latérale |
| `--surface` / `--surface-2` | `bg-surface` / `bg-surface-2` | cartes / zones en retrait |
| `--border` / `--border-strong` | `border-border` / `border-border-strong` | traits |
| `--text` / `--muted` / `--faint` | `text-fg` / `text-muted` / `text-faint` | textes (`text-faint` reste ≥ 4,5:1 sur le fond : réservé aux textes secondaires ; un texte porteur d'information ≤ 12 px utilise `text-muted`) |
| `--accent` / `--accent-soft` | `bg-accent`, `text-accent`, `bg-accent-soft` | primaire, actif |
| `--success` `--warning` `--danger` `--info` | `text-success`, `bg-danger/12`… | statuts |
| `--hover` / `--active` | `hover:bg-hover`, `bg-active` | survol / sélection |
| `--chart-1…6` | — (charts) | palette catégorielle validée (ordre fixe) |

Utilitaires : `.drag` / `.no-drag` (zone de déplacement de la fenêtre — tout élément
interactif placé dans une zone `.drag` doit porter `.no-drag`), `.tabular`,
`.selectable` (le texte est non sélectionnable par défaut, sauf inputs/pre/code).
Thème : `useResolvedTheme()`, `useThemePreference()` / `setThemePreference("system"|"light"|"dark")`
dans `lib/theme.ts` (pour la page Réglages).

## 3. Mise en page

```tsx
<Page width="default|wide|full">          // max 1200 px (wide : 1480 px), gouttières 40 px
  <PageHeader title eyebrow? description? back?={{to,label}} actions? meta? />
  <div className="space-y-10">
    <Section title description? actions?> … </Section>
  </div>
</Page>
```

- Section n'a pas de marge externe : espacer avec `space-y-10` ou `mt-10`.
- Grilles de KPI : `grid grid-cols-2 gap-4 lg:grid-cols-4`.
- Onglets de page : état dans l'URL (`?tab=`), voir `pages/RunDetail.tsx`.
- Filtres de liste : dans l'URL (`useSearchParams`), voir `pages/RunsList.tsx`.
- `AppShell` (sidebar + contenu + bande draggable de 52 px) et `data-app-ready` sont
  gérés globalement : une page ne s'occupe que de son contenu.

## 4. Composants (`src/components/ui`, import depuis `../components/ui`)

| composant | props principales |
|---|---|
| `Button` | `variant` primary/secondary/ghost/danger, `size` sm/md, `icon`, `iconRight`, `loading`, `to` (lien routeur), `kbd` ("⌘S"), `fullWidth` |
| `IconButton` | `icon`, `label` (aria + tooltip), `size`, `variant` ghost/secondary, `active`, `tooltipSide` |
| `Card`, `CardHeader` | `padding` none/sm/md/lg, `interactive`, `muted` ; header : `title`, `description`, `icon`, `actions` |
| `Badge`, `StatusBadge` | `tone` neutral/accent/success/warning/danger/info, `dot`, `pulse`, `mono` ; `StatusBadge status` (RunStatus/JobStatus, point animé si running), `dotOnly` |
| `Tabs`, `Segmented` | `items` {id,label,icon?,count?} + `value`/`onChange` ; `Segmented options` pour 2–4 choix |
| `Table<T>` | `columns` {id, header, cell, sortValue?, align?, width?, wrap?, title?}, `rows`, `rowKey`, `onRowClick`, `dense`, `initialSort`, `selectable`/`selected`/`onSelectionChange`, `loading`, `empty`, `maxHeight`, `bare` |
| `Input`, `Textarea`, `Select`, `Switch`, `Field` | `label`, `hint`, `error` ; Input : `icon`, `suffix`, `mono`, `size` ; Select : `options` {value,label,disabled?}, `value`, `onChange(value)`, `placeholder` ; Switch : `checked`, `onChange(bool)`, `label`, `description` |
| `Dialog` (= `Modal`), `Drawer` | `open`, `onClose`, `title`, `description`, `footer`, `size` sm/md/lg/xl ; Drawer : `width` |
| `ConfirmDialog`, `useConfirm()` | `await confirm({title, message, confirmLabel, tone: "danger"})` → booléen |
| `Toast` | `const toast = useToast(); toast.success(titre, détail?)`, `.error`, `.info`, `.warning` |
| `Tooltip` | `content`, `side`, `wrapperClassName` (remplace `inline-flex`) |
| `EmptyState` | `icon`, `title`, `description`, `action`, `compact` |
| `Kpi` | `label`, `value`, `sub`, `icon`, `progress` (0..1), `hint`, `loading`, `onClick` |
| `KeyValue` | `items` {label, value, mono?}, `columns` 1/2/3, `dense` |
| `ProgressBar`, `Skeleton`, `PageSkeleton`, `Spinner`, `LoadingBlock` | — |
| `Callout` | `tone`, `icon`, `title`, `action` (bandeaux d'erreur / d'information) |
| `CodeBlock`, `copyText()` | `code`, `title`, `maxHeight`, `wrap` |
| `Markdown` | `source`, `empty` — rendu sûr sans dépendance (titres, listes, tâches, tableaux, code, liens) |
| `ModalityDot`, `Kbd` | pastille de modalité ; touche clavier |

Composants métier (`src/components`) :

- `PhaseTimeline` — `plan: PhasePlanPreview`, `currentStep?`, `live?`, `curves?`, `attacks?` : LA
  visualisation du curriculum (barre proportionnelle + courbes LR / force / poids + attaques, survol).
- `YamlEditor` — `value`, `onChange`, `onSave` (⌘S), `readOnly`, `errors`, `valid`, `height`/`minHeight`/`maxHeight`.
  Pour la validation en direct : `useValidation({content, kind, path})` (débouncée) → `errors`, `plan`.
- `RunBits` — `RunProgress` (progression + phase + ETA), `MetricValue`, `MetricList`, `TagEditor`, `JobList`.
- `NewProjectDialog` — `open`, `onClose`.
- `launch/LaunchProvider` — `const { openLaunch } = useLaunch(); openLaunch({ kind, project, experiment, plan, host, overrides, smoke, maxHours, initFrom, evalRun, evalCheckpoint, benchmark })`.
  Le dialogue porte le champ « Révision git » (pré-rempli `branche @ sha` depuis `useGit()`,
  `gitRefForLaunch()` décide du `git_ref` envoyé) et n'offre jamais une machine retirée.
- `LoginGate` (mode équipe, §12.6) — enveloppe l'app : écran de connexion tant qu'aucun jeton n'est
  accepté (navigateur), écran « connexion requise / jeton refusé » en Electron distant, passage
  direct en Electron local. `const { logout } = useSession()` pour « Se déconnecter ».
- `hosts/HostInventoryPanel` — `host`, `enabled` : inventaire d'une machine (runs présents,
  checkpoints non rapatriés, jobs actifs, état de l'instance Scaleway) ; `instanceStateLabel()`.
- `hosts/DecommissionDialog` — `host`, `activeJobs`, `onClose` : « Retirer la machine » (récapitulatif
  vérifié, gardes `decommissionBlockers()`, case « supprimer aussi l'instance Scaleway », `force` si injoignable).
- `saveToast.toastFileSaved(toast, res, titre, détail?)` — toast après `PUT /api/files` : « Enregistré ·
  commité abc1234 · poussé » ou avertissement « commité mais non poussé ».

Graphiques (`src/components/charts`) — thème dérivé des tokens, re-rendu clair/sombre automatique :

| composant | props principales |
|---|---|
| `LineChart` | `series` {name, x, y, color?, dashed?, area?, step?}, `phases` (bandes nommées), `logScale`, `xType` value/time (ms), `zoom` inside/slider/false, `markX`, `referenceLines`, `yFormatter`, `valueFormatter` |
| `ScatterPareto` | `points` {id, x, y, label?, group?, meta?}, `xLabel`/`yLabel`, `xDirection`/`yDirection`, `xConstraint`/`yConstraint` (`parseConstraint(">= 38")`), `highlightId`, `onPointClick` |
| `Heatmap` | `rows`, `columns`, `values[row][col]`, `formatter`, `min`/`max`, `direction`, `onCellClick` |
| `ParallelCoords` | `dimensions` {key, label, type?, categories?, log?, inverse?}, `rows` {id, values, highlight?}, `colorBy` |
| `BarChart` | `categories`, `series` {name, values}, `horizontal`, `stacked`, `valueFormatter` |
| `Sparkline` | `values`, `width`, `height`, `color` (SVG pur, pour les cellules de tableau) |
| `EChart` | wrapper bas niveau : `option`, `height`, `onEvents`, `notMerge`, `onReady` |

Règles : une seule échelle Y par graphique ; couleurs de séries dans l'ordre fixe de la
palette (`seriesColor(t, i)`), au-delà de 6 séries les lignes passent en pointillés ;
texte toujours en couleurs de texte, jamais en couleur de série.

## 5. Données (`src/lib`)

- `types.ts` : toutes les interfaces du §9 + corps/réponses des routes (`CreateJobBody`, `ProjectDetail`…).
- `api.ts` : `api.<route>()` pour chaque route (§9 et §12 : `me`, `listUsers`, `git`, `gitPull`,
  `hostInventory`, `retrieveHost`, `decommissionHost`, `poweroffHost`, `hostInstance`),
  `fileUrl(runId, path)`, `costsCsvUrl()`, `downloadCostsCsv()`, `ApiError` (`status`, `detail`),
  `errorMessage(err)`. Session : `getApiConfig()` (`apiBase`, `token`, `source` electron/vite-env/browser,
  `mode` local/remote), `probeConnection(apiBase, token)` (`/api/health` puis `/api/me`, ne lève jamais),
  `setSession()` / `clearSession()` (localStorage `wmlab.token` / `wmlab.apiBase`, toujours en
  try/catch), `normalizeApiBase()` ; tout 401 émet l'événement `UNAUTHORIZED_EVENT` sur `window`.
- `queries.ts` : un hook par lecture (`useOverview`, `useProjects`, `useProject`, `useFile`,
  `useValidation`, `useExperimentPlan`, `useRegistry`, `useRuns`, `useRun`, `useRunMetrics`,
  `useRunSystem`, `useRunLogs`, `useCompare`, `useStudies`, `useStudy`, `useLeaderboard`,
  `useBenchmarks`, `useDatasets`, `useHosts`, `useJobs`, `useJobEstimate`, `useCosts`, `useSettings`,
  et §12 : `useMe` / `useCurrentUser`, `useUsers`, `useGit`, `useHostInventory`, `useHostInstance`)
  et par écriture (`useCreateProject`, `useCreateExperiment`, `useCreateHpoPlan`, `useSaveFile`,
  `useValidate`, `useSetRunNotes`, `useSetStudyNotes`, `useSetRunTags`, `useStopRun`, `useFetchCheckpoint`,
  `useSaveHosts`, `useTestHost`, `useSetupHost`, `useCreateJob`, `useEstimateJob`, `useCancelJob`,
  `useSyncJob`, `useSaveSettings`, et §12 : `useGitPull`, `useRetrieveHost`, `useDecommissionHost`,
  `usePoweroffHost`) ; invalidations déjà câblées ; `errorStatus(err)` donne le statut HTTP d'une
  erreur. `useMe` n'est jamais relancé (`retry: 0`) : un 401 rend la main au `LoginGate`, un 404
  (backend antérieur au §12) laisse l'app utilisable avec un utilisateur inconnu. Polling (`refetchInterval`,
  constantes `LIVE_INTERVAL` = 5 s et `IDLE_INTERVAL` = 30 s dans `queries.ts`) : les listes
  (`useOverview`, `useRuns`, `useStudies`, `useJobs`) et `useProjects` passent en rythme
  « live » (5 s ; projets 10 s) tant qu'un élément est actif (run / étude / job en cours), sinon
  30 s (jobs : 15 s) ; `useRun` / `useStudy` 5 s si actif, sinon aucun polling ; métriques,
  système, logs et comparaison d'un run en mode `live` : 5–10 s (logs 3 s) ; `useProject` et
  `useHealth` 15 s ; `useLeaderboard` / `useCosts` 30 s ; le reste (registre, benchmarks,
  datasets, machines, fichiers) sans polling, invalidé par les mutations. Rien n'est interrogé
  quand la fenêtre est en arrière-plan (`refetchIntervalInBackground: false`) ; refetch au
  retour du focus.
- `format.ts` : `formatEur` (« 1 234,56 € »), `formatDuration` (« 2 h 14 min »), `formatRelative`
  (« il y a 3 min »), `formatDate`, `formatNumber`, `formatInt`, `formatPercent`, `formatBytes`,
  `formatSteps`, `formatHours`, `formatMonth`, `plural`, `shortId`, `toDate`.
- `metrics.ts` : `metricLabel(key, {withGroup})`, `metricDirection`, `formatMetric`,
  `formatMetricAxis`, `groupMetricKeys`, `qualityMetricFor(modality)`, `pickKeyMetrics`,
  `paretoFront`, `smoothSeries`, `parseConstraint`, `satisfies`.
- `runs.ts` : `STATUS_LABELS`, `MODALITY_LABELS`, `JOB_KIND_LABELS`, `isActiveStatus`,
  `progressFraction`, `runEta`, et les routes `runPath`, `studyPath`, `projectPath`,
  `experimentPath`, `hpoPlanPath`.
- `hooks.ts` : `useHotkey("mod+s", fn)`, `useDebouncedValue`, `useLocalStorage`, `useNow`.
- `hosted.ts` (mode équipe, sans React, testé dans `tests/hosted.test.ts`) : `gitRevisionLabel(git)`
  (« main @ 3f2a9c1 »), `gitRefForLaunch(valeur, libellé, git)` (`git_ref` à envoyer : sha figé en mode
  hébergé, rien hors mode hébergé si le champ est intact), `describeFileSave(git)` (contenu du toast
  après enregistrement), `retrievalNotice(retrieval)` / `hasRetrievalIssue(job)` (avertissement
  `partial` / `failed`), `describeRetrieveResult(result)` (« 12 runs · 1 étude · 37 checkpoints · 18 Go »),
  `decommissionBlockers(host, activeJobs)` (raisons qui bloquent « Retirer la machine », miroir des 409 du serveur).

## 6. Patrons de page

- **Chargement** : `Skeleton`/`PageSkeleton` (jamais d'écran vide) ; **erreur** : `Callout tone="danger"`
  avec `errorMessage(err)` et un bouton « Réessayer » (`refetch`) ; **vide** : `EmptyState` avec une action.
- **Actions destructives** : `useConfirm()` + `toast` de résultat ; boutons `variant="danger"`.
- **Formulaires** en dialogue : validation locale avant l'appel, erreurs serveur dans un `Callout`.
- **Liens** vers runs / études / projets via `runPath()` etc. ; liens externes en `<a target="_blank">`
  (ouverts dans le navigateur par Electron, seuls `http(s):` et `mailto:` passent).
- **Mode équipe** : l'auteur s'affiche en « par <nom> » sous l'identifiant (runs, études, jobs, en-tête
  du run) ; les boutons réservés aux administrateurs (`useMe().data?.role === "admin"`) restent visibles
  quand l'utilisateur est inconnu (le serveur tranche en 403) ; un job dont `retrieval` est `partial` /
  `failed` porte un avertissement `text-warning` (listes) ou un `Callout tone="warning"` (page du run)
  qui renvoie vers la page Machines ; un champ optionnel du contrat (`user`, `git_sha`, `scaleway`…)
  est toujours lu avec `?.` / `??` pour tolérer un backend plus ancien.

## 7. Conventions de texte

- Tutoiement exclu, phrases courtes, pas de point final dans les boutons (« Nouveau lancement »).
- Majuscule initiale seulement (« Runs récents », pas « Runs Récents »).
- Statuts : En file, Lancement, En cours, Terminé, Échec, Arrêté, Perdu.
- Mode équipe : « Rapatrier les checkpoints », « Retirer la machine », « Éteindre l'instance »,
  « Se connecter », « Se déconnecter », « Tirer maintenant », « par <nom> », « Révision git » ;
  instance Scaleway : en marche, éteinte, arrêtée (volumes conservés), extinction…, démarrage….
- Unités : « 38,20 dB », « 93,4 % », « 12,5 h », « 1,40 €/h », « 46 Mo ».
- Termes consacrés conservés en anglais quand usuels : run, checkpoint, benchmark, step, smoke, override.
