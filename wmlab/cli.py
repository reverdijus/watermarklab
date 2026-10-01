"""Command line interface ``wmlab`` (ARCHITECTURE.md §8.4)."""

from __future__ import annotations

import importlib
import json
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from wmlab import __version__

app = typer.Typer(
    name="wmlab",
    help="Watermark Lab — entraîner, évaluer et suivre des schémas de watermarking "
         "image / audio / vidéo.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
new_app = typer.Typer(help="Créer un projet, une expérience, un plan HPO ou un squelette de "
                           "plugin.", no_args_is_help=True)
runs_app = typer.Typer(help="Lister et inspecter les runs.", no_args_is_help=True)
users_app = typer.Typer(help="Utilisateurs et jetons du mode hébergé (.wmlab/users.yaml).",
                        no_args_is_help=True)
weights_app = typer.Typer(help="Poids officiels pré-entraînés des baselines (hors dépôt).",
                          no_args_is_help=True)
app.add_typer(new_app, name="new")
app.add_typer(runs_app, name="runs")
app.add_typer(users_app, name="users")
app.add_typer(weights_app, name="weights")

STATUS_STYLES = {
    "running": "cyan", "queued": "dim", "completed": "green", "failed": "red",
    "killed": "yellow", "lost": "magenta",
}


# -- helpers --------------------------------------------------------------------------------
def _out() -> Console:
    return Console(highlight=False, soft_wrap=False)


def _err() -> Console:
    return Console(stderr=True, highlight=False)


def _fail(message: str, code: int = 1) -> None:
    _err().print(f"[bold red]Erreur :[/bold red] {escape(message)}")
    raise typer.Exit(code)


def _echo_json(obj: Any) -> None:
    from wmlab.tracking.io import sanitize

    typer.echo(json.dumps(sanitize(obj), indent=2, ensure_ascii=False))


def _parse_sets(items: list[str] | None) -> dict[str, str]:
    from wmlab.spec import OverrideError, parse_set_options

    try:
        return parse_set_options(items)
    except OverrideError as exc:
        _fail(str(exc))
    return {}


def _delegate(module: str, func: str) -> Callable[..., Any]:
    try:
        return getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError) as exc:
        _fail(f"fonctionnalité indisponible : impossible d'importer {module}.{func} ({exc})")
    raise AssertionError  # unreachable


def _run_delegated(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    from wmlab.registry import UnknownComponentError
    from wmlab.spec import OverrideError, SpecError
    from wmlab.tracking import CheckpointNotFoundError

    expected = (SpecError, OverrideError, FileNotFoundError, FileExistsError,
                UnknownComponentError, CheckpointNotFoundError)
    try:
        return fn(*args, **kwargs)
    except KeyboardInterrupt:
        _err().print("[yellow]Interrompu.[/yellow]")
        raise typer.Exit(130)
    except expected as exc:
        _fail(str(exc))
    except typer.Exit:
        raise
    except Exception as exc:  # full traceback is useful in stdout.log
        traceback.print_exc()
        _fail(f"{type(exc).__name__}: {exc}")
    return None


def _print_summary(title: str, summary: Any) -> None:
    console = _out()
    if not isinstance(summary, dict) or not summary:
        console.print(f"[green]✓[/green] {escape(title)}")
        return
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("Clé")
    table.add_column("Valeur", justify="right")
    for key, value in summary.items():
        if isinstance(value, float):
            value = f"{value:.4g}"
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)[:80]
        table.add_row(escape(str(key)), escape(str(value)))
    console.print(table)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wmlab {__version__}")
        raise typer.Exit()


@app.callback()
def _main(version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Afficher la version et quitter.")) -> None:
    """Watermark Lab."""


# -- engine commands (delegated) ------------------------------------------------------------
@app.command(help="Démarrer le backend HTTP de l'application (écoute 127.0.0.1).")
def server(
    port: int = typer.Option(8765, "--port", help="Port HTTP."),
    token: Optional[str] = typer.Option(None, "--token", help="Jeton exigé dans l'en-tête "
                                        "X-WMLab-Token (sinon variable WMLAB_TOKEN, sinon un "
                                        "jeton aléatoire est généré et affiché au démarrage)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Adresse d'écoute (hors 127.0.0.1 : "
                                                         "jeton explicite obligatoire)."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", help="Racine du workspace."),
    no_sync: bool = typer.Option(False, "--no-sync", help="Désactiver la synchronisation des "
                                                          "runs distants."),
) -> None:
    if workspace is not None:
        workspace = workspace.expanduser().resolve()
        os.environ["WMLAB_WORKSPACE"] = str(workspace)
    else:
        from wmlab.paths import workspace_root

        try:
            workspace = workspace_root()
        except Exception as exc:
            _fail(str(exc))
    if host not in ("127.0.0.1", "localhost", "::1") and not (
            (token or "").strip() or (os.environ.get("WMLAB_TOKEN") or "").strip()):
        _fail(f"écoute sur {host} : un jeton explicite est requis (--token ou variable "
              "WMLAB_TOKEN) pour exposer l'API hors de cette machine.")
    serve = _delegate("wmlab.server.app", "serve")
    _run_delegated(serve, port=port, token=token, host=host, workspace=workspace,
                   sync=not no_sync)


@app.command(help="Lancer un entraînement à partir d'une expérience YAML.")
def train(
    spec: Path = typer.Argument(..., help="Fichier d'expérience (projects/<p>/experiments/<e>.yaml)."),
    set_: Optional[list[str]] = typer.Option(None, "--set", help="Override clé=valeur "
                                             "(répétable), ex. --set optimizer.lr=3e-4."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Identifiant de run imposé."),
    smoke: bool = typer.Option(False, "--smoke", help="Essai rapide (données synthétiques, "
                                                      "quelques steps)."),
    device: Optional[str] = typer.Option(None, "--device", help="auto, cpu, mps, cuda…"),
    max_hours: Optional[float] = typer.Option(None, "--max-hours", help="Durée maximale "
                                                                        "(checkpoint puis arrêt)."),
    launch_file: Optional[str] = typer.Option(None, "--launch-file", help="Fichier de lancement "
                                                                          "écrit par l'app."),
) -> None:
    overrides = _parse_sets(set_)
    fn = _delegate("wmlab.train.trainer", "train_from_spec")
    summary = _run_delegated(fn, spec, overrides=overrides or None, run_id=run_id, smoke=smoke,
                             device=device, max_hours=max_hours, launch_file=launch_file)
    _print_summary("Entraînement terminé", summary)


@app.command(help="Lancer une étude d'optimisation d'hyperparamètres (plan HPO YAML).")
def hpo(
    spec: Path = typer.Argument(..., help="Plan HPO (projects/<p>/hpo/<plan>.yaml)."),
    set_: Optional[list[str]] = typer.Option(None, "--set", help="Override clé=valeur appliqué "
                                             "à l'expérience de base de chaque essai (répétable)."),
    study_id: Optional[str] = typer.Option(None, "--study-id", help="Identifiant d'étude imposé."),
    smoke: bool = typer.Option(False, "--smoke", help="Essai rapide (3 essais, trainer en smoke)."),
    device: Optional[str] = typer.Option(None, "--device", help="auto, cpu, mps, cuda…"),
    max_hours: Optional[float] = typer.Option(None, "--max-hours", help="Durée maximale de "
                                              "l'étude (plafonne budget.max_hours)."),
    max_trials: Optional[int] = typer.Option(None, "--max-trials", help="Nombre maximal "
                                             "d'essais (plafonne budget.max_trials)."),
    launch_file: Optional[str] = typer.Option(None, "--launch-file", help="Fichier de lancement."),
) -> None:
    overrides = _parse_sets(set_)
    fn = _delegate("wmlab.hpo.driver", "run_study")
    result = _run_delegated(fn, spec, study_id=study_id, smoke=smoke, device=device,
                            launch_file=launch_file, overrides=overrides or None,
                            max_hours=max_hours, max_trials=max_trials)
    _print_summary("Étude terminée", result)


@app.command("hpo-worker", help="Worker d'étude HPO (lancé par « wmlab hpo »).")
def hpo_worker(
    study_dir: Path = typer.Argument(..., help="Dossier de l'étude."),
    worker_index: int = typer.Option(..., "--worker-index", help="Index du worker."),
    device: Optional[str] = typer.Option(None, "--device", help="auto, cpu, mps, cuda…"),
) -> None:
    fn = _delegate("wmlab.hpo.driver", "run_worker")
    _run_delegated(fn, study_dir, worker_index=worker_index, device=device)


@app.command("eval", help="Évaluer un checkpoint d'un run sur un benchmark figé.")
def eval_(
    run: str = typer.Option(..., "--run", help="Identifiant ou dossier du run."),
    checkpoint: str = typer.Option(..., "--checkpoint", help="last, best:<métrique>, "
                                                             "phase_end:<phase> ou nom."),
    benchmark: str = typer.Option(..., "--benchmark", help="Nom du benchmark (benchmarks/<nom>.yaml)."),
    device: Optional[str] = typer.Option(None, "--device", help="auto, cpu, mps, cuda…"),
    smoke: bool = typer.Option(False, "--smoke", help="Évaluation rapide (8 items)."),
) -> None:
    fn = _delegate("wmlab.eval.benchmark", "evaluate_checkpoint")
    result = _run_delegated(fn, run, checkpoint, benchmark, device=device, smoke=smoke)
    summary = result.get("summary") if isinstance(result, dict) else result
    _print_summary(f"Évaluation {benchmark}", summary)


@app.command(help="Reconstruire l'index SQLite du serveur à partir des dossiers de runs.")
def index() -> None:
    from wmlab.paths import workspace_root

    try:
        workspace = workspace_root()
    except Exception as exc:
        _fail(str(exc))
    fn = _delegate("wmlab.server.indexer", "reindex_workspace")
    result = _run_delegated(fn, workspace)
    _print_summary("Index reconstruit", result if isinstance(result, dict) else {})


# -- validation -----------------------------------------------------------------------------
def _collect_targets(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*")
                                if p.suffix in (".yaml", ".yml") and ".wmlab" not in p.parts))
        else:
            files.append(path)
    return files


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


@app.command(help="Valider des fichiers YAML (sans argument : tout le workspace).")
def validate(
    paths: Optional[list[Path]] = typer.Argument(None, help="Fichiers ou dossiers à valider."),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON."),
) -> None:
    from wmlab.registry import last_report
    from wmlab.spec import validate_file, validate_workspace
    from wmlab.spec.validate import plugin_error_results

    if paths:
        results = {_display_path(p): validate_file(p) for p in _collect_targets(paths)}
        report = last_report()
        if report is not None:  # a broken lab/ plugin is an error even with explicit paths
            try:
                from wmlab.paths import workspace_root

                root = workspace_root()
            except Exception:
                root = None
            if root is not None:
                for rel, errors in plugin_error_results(report, root).items():
                    results.setdefault(rel, []).extend(errors)
    else:
        try:
            results = validate_workspace()
        except Exception as exc:
            _fail(str(exc))
    report = last_report()
    builtin_errors = [e for e in (report.errors if report else [])
                      if not e["module"].startswith("lab")]
    n_invalid = sum(1 for errors in results.values() if errors)
    if as_json:
        _echo_json({"valid": n_invalid == 0, "files": results, "plugin_errors": builtin_errors})
        raise typer.Exit(1 if n_invalid else 0)
    console = _out()
    if not results:
        console.print("[dim]Aucun fichier à valider.[/dim]")
    for path, errors in results.items():
        if not errors:
            console.print(f"[green]✓[/green] {escape(path)}")
            continue
        console.print(f"[red]✗[/red] {escape(path)}")
        for error in errors:
            loc = error.get("loc") or "(fichier)"
            console.print(f"    [bold]{escape(loc)}[/bold] : {escape(error.get('msg', ''))}")
    for error in builtin_errors:
        last = error["error"].strip().splitlines()[-1] if error["error"].strip() else ""
        console.print(f"[yellow]Avertissement :[/yellow] module {escape(error['module'])} non "
                      f"chargé ({escape(last)})")
    n_valid = len(results) - n_invalid
    style = "red" if n_invalid else "green"
    console.print(f"[{style}]{n_valid} fichier(s) valide(s), {n_invalid} invalide(s)[/{style}]")
    raise typer.Exit(1 if n_invalid else 0)


# -- registry & plugin test -----------------------------------------------------------------
@app.command(help="Lister les composants enregistrés (modèles, attaques, datasets…).")
def registry(
    kind: Optional[str] = typer.Option(None, "--kind", help="model, attack, dataset, "
                                                            "preprocess, loss ou metric."),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON."),
) -> None:
    from wmlab.registry import REGISTRIES, get_registry, load_plugins

    report = load_plugins()
    if kind is not None:
        try:
            kinds = [get_registry(kind).kind]
        except ValueError as exc:
            _fail(str(exc))
    else:
        kinds = list(REGISTRIES)
    if as_json:
        _echo_json({"entries": {k: REGISTRIES[k].describe() for k in kinds},
                    "errors": report.errors})
        return
    console = _out()
    for k in kinds:
        entries = REGISTRIES[k].list()
        table = Table(title=f"{k} ({len(entries)})", title_justify="left", header_style="bold")
        table.add_column("Nom", no_wrap=True)
        table.add_column("Modalité")
        table.add_column("Origine")
        table.add_column("Description")
        for e in entries:
            origin = "builtin" if e.builtin else f"plugin ({e.source})"
            table.add_row(escape(e.name), escape(e.modality_label()), escape(origin),
                          escape(e.description))
        console.print(table)
    for error in report.errors:
        last = error["error"].strip().splitlines()[-1] if error["error"].strip() else ""
        console.print(f"[red]✗ import {escape(error['module'])}[/red] : {escape(last)}")


@app.command("plugin-test", help="Vérifier un composant (formes, plages, gradients).")
def plugin_test(
    kind: str = typer.Argument(..., help="model, attack, dataset, preprocess, loss ou metric."),
    name: str = typer.Argument(..., help="Nom enregistré du composant."),
    modality: Optional[str] = typer.Option(None, "--modality", help="Modalité testée "
                                                                    "(défaut : la première "
                                                                    "supportée)."),
    param: Optional[list[str]] = typer.Option(None, "--param", help="Paramètre clé=valeur du "
                                              "composant testé (répétable), ex. --param "
                                              "root=~/images pour un dataset dossier."),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON."),
) -> None:
    from wmlab.plugin_test import run_plugin_test

    from wmlab.spec import parse_override_value

    params = {k: parse_override_value(v) for k, v in _parse_sets(param).items()}
    results = run_plugin_test(kind, name, modality, params=params or None)
    failed = [r for r in results if not r["ok"]]
    if as_json:
        _echo_json({"ok": not failed, "checks": results})
        raise typer.Exit(1 if failed else 0)
    table = Table(title=f"plugin-test {kind} {name}", title_justify="left", header_style="bold")
    table.add_column("Vérification")
    table.add_column("", justify="center")
    table.add_column("Détail")
    for r in results:
        mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
        table.add_row(escape(r["check"]), mark, escape(str(r["detail"])))
    console = _out()
    console.print(table)
    if failed:
        console.print(f"[red]{len(failed)} vérification(s) en échec[/red]")
        raise typer.Exit(1)
    console.print("[green]Toutes les vérifications passent.[/green]")


# -- scaffolding ----------------------------------------------------------------------------
def _created(path: Path, hint: str) -> None:
    console = _out()
    console.print(f"[green]Créé :[/green] {escape(_display_path(path))}")
    console.print(f"[dim]{escape(hint)}[/dim]")


def _scaffold(fn: Callable[..., Path], *args: Any, **kwargs: Any) -> Path:
    from wmlab.scaffold import ScaffoldError

    try:
        return fn(*args, **kwargs)
    except (ScaffoldError, FileExistsError, ValueError) as exc:
        _fail(str(exc))
    raise AssertionError  # unreachable


@new_app.command("project", help="Créer un projet (project.yaml + expérience baseline).")
def new_project(
    name: str = typer.Argument(..., help="Nom du projet (dossier projects/<nom>)."),
    modality: str = typer.Option(..., "--modality", help="image, audio ou video."),
    bits: int = typer.Option(48, "--bits", help="Taille du message (message_bits)."),
    title: Optional[str] = typer.Option(None, "--title", help="Titre affiché."),
    description: str = typer.Option("", "--description", help="Description."),
) -> None:
    from wmlab import scaffold

    path = _scaffold(scaffold.new_project, name, modality, bits, title=title,
                     description=description)
    _created(path, "Expérience de départ : "
                   f"{_display_path(path.parent / 'experiments' / 'baseline.yaml')} — vérifier "
                   f"avec « wmlab validate {_display_path(path.parent)} »")


@new_app.command("experiment", help="Créer une expérience (gabarit commenté ou copie).")
def new_experiment(
    project: str = typer.Argument(..., help="Projet existant."),
    name: str = typer.Argument(..., help="Nom de l'expérience."),
    from_: Optional[str] = typer.Option(None, "--from", help="Expérience à copier."),
) -> None:
    from wmlab import scaffold

    path = _scaffold(scaffold.new_experiment, project, name, from_experiment=from_)
    _created(path, f"Vérifier : wmlab validate {_display_path(path)} — essai rapide : "
                   f"wmlab train {_display_path(path)} --smoke")


@new_app.command("plugin", help="Générer un squelette de plugin dans lab/<kind>s/.")
def new_plugin(
    kind: str = typer.Argument(..., help="model, attack, dataset, preprocess, loss ou metric."),
    name: str = typer.Argument(..., help="Nom du composant (identifiant Python)."),
    modality: str = typer.Option(..., "--modality", help="image, audio, video, liste "
                                                         "« image,video » ou « all »."),
) -> None:
    from wmlab import scaffold

    path = _scaffold(scaffold.new_plugin, kind, name, modality)
    _created(path, f"Compléter les TODO puis vérifier : wmlab plugin-test {kind} {name}")


@new_app.command("hpo", help="Créer un plan HPO pour une expérience.")
def new_hpo(
    project: str = typer.Argument(..., help="Projet existant."),
    name: str = typer.Argument(..., help="Nom du plan."),
    experiment: str = typer.Option(..., "--experiment", help="Expérience de base."),
) -> None:
    from wmlab import scaffold

    path = _scaffold(scaffold.new_hpo, project, name, experiment)
    _created(path, f"Vérifier : wmlab validate {_display_path(path)} — essai rapide : "
                   f"wmlab hpo {_display_path(path)} --smoke")


# -- runs -----------------------------------------------------------------------------------
def _resolve_run_dir(run: str) -> Path:
    from wmlab.paths import runs_dir
    from wmlab.tracking import list_runs

    candidate = Path(run).expanduser()
    if (candidate / "run.json").is_file():
        return candidate.resolve()
    base = runs_dir()
    if (base / run / "run.json").is_file():
        return base / run
    matches = [p for p in list_runs(base) if run in p.name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        _fail(f"run introuvable : '{run}' (dossier {base})")
    _fail(f"'{run}' est ambigu : {', '.join(p.name for p in matches[:10])}"
          + (" …" if len(matches) > 10 else ""))
    raise AssertionError  # unreachable


def _progress_text(progress: Any) -> str:
    if not isinstance(progress, dict) or not progress.get("total_steps"):
        return "—"
    step, total = progress.get("step", 0) or 0, progress.get("total_steps") or 0
    pct = 100.0 * step / total if total else 0.0
    return f"{step}/{total} ({pct:.0f} %) {progress.get('phase') or ''}".strip()


def _status_text(status: str) -> str:
    return f"[{STATUS_STYLES.get(status, 'white')}]{status}[/]"


@runs_app.command("list", help="Lister les runs (plus récents d'abord).")
def runs_list(
    project: Optional[str] = typer.Option(None, "--project", help="Filtrer par projet."),
    status: Optional[str] = typer.Option(None, "--status", help="Filtrer par statut (running, "
                                                                "completed, failed, killed, lost…)."),
    include_trials: bool = typer.Option(False, "--include-trials", help="Inclure les runs "
                                                                        "d'essais HPO."),
    limit: int = typer.Option(100, "--limit", help="Nombre maximal de runs."),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON."),
) -> None:
    from wmlab.paths import runs_dir
    from wmlab.tracking import RunReader, list_runs

    try:
        base = runs_dir()
    except Exception as exc:
        _fail(str(exc))
    manifests: list[dict[str, Any]] = []
    for run_dir in list_runs(base):
        try:
            manifest = RunReader(run_dir).manifest()
        except Exception:
            continue
        if project and manifest.get("project") != project:
            continue
        if status and manifest.get("status") != status:
            continue
        if not include_trials and manifest.get("study"):
            continue
        manifests.append(manifest)
        if len(manifests) >= limit:
            break
    if as_json:
        _echo_json(manifests)
        return
    console = _out()
    if not manifests:
        console.print("[dim]Aucun run.[/dim]")
        return
    table = Table(header_style="bold")
    table.add_column("Run", no_wrap=True)
    table.add_column("Projet")
    table.add_column("Statut", no_wrap=True)
    table.add_column("Avancement", justify="right", no_wrap=True)
    table.add_column("Coût €", justify="right", no_wrap=True)
    for m in manifests:
        progress = m.get("progress") if isinstance(m.get("progress"), dict) else {}
        total = progress.get("total_steps") or 0
        done = f"{100.0 * (progress.get('step') or 0) / total:.0f} %" if total else "—"
        table.add_row(escape(str(m.get("run_id"))), escape(str(m.get("project") or "")),
                      _status_text(str(m.get("status"))), done, f"{m.get('cost_eur', 0.0):.2f}")
    console.print(table)


@runs_app.command("show", help="Afficher un run : manifest, résumé, checkpoints, évaluations.")
def runs_show(
    run: str = typer.Argument(..., help="Identifiant (ou fragment unique) ou dossier du run."),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON (pour l'analyse)."),
) -> None:
    from wmlab.tracking import RunReader

    reader = RunReader(_resolve_run_dir(run))
    try:
        manifest = reader.manifest()
    except FileNotFoundError as exc:
        _fail(str(exc))
    data = {
        "run_dir": str(reader.dir),
        "manifest": manifest,
        "summary": manifest.get("summary") or {},
        "checkpoints": reader.checkpoints(),
        "evals": reader.evals(),
        "metric_keys": reader.metric_keys(),
        "last_values": reader.last_values(),
        "notes": reader.notes(),
    }
    if as_json:
        _echo_json(data)
        return
    console = _out()
    console.print(f"[bold]{escape(manifest.get('run_id', ''))}[/bold]  "
                  f"{_status_text(str(manifest.get('status')))}")
    info = Table(show_header=False, box=None, pad_edge=False)
    info.add_column(style="dim")
    info.add_column()
    duration = manifest.get("duration_s")
    for label, value in (
        ("Projet", manifest.get("project")),
        ("Expérience", manifest.get("experiment")),
        ("Modalité", manifest.get("modality")),
        ("Spec", manifest.get("spec_path")),
        ("Hôte", f"{manifest.get('host')} ({manifest.get('price_eur_per_hour', 0)} €/h)"),
        ("Durée", f"{duration / 3600:.2f} h" if isinstance(duration, (int, float)) else "—"),
        ("Coût", f"{manifest.get('cost_eur', 0.0):.2f} €"),
        ("Progression", _progress_text(manifest.get("progress"))),
        ("Parent", json.dumps(manifest.get("parent")) if manifest.get("parent") else "—"),
        ("Étude", f"{manifest.get('study')} (essai {manifest.get('trial')})"
         if manifest.get("study") else "—"),
        ("Erreur", (manifest.get("error") or "—").strip().splitlines()[-1]
         if manifest.get("error") else "—"),
    ):
        info.add_row(label, escape(str(value)) if value not in (None, "") else "—")
    console.print(info)
    _print_summary("Résumé", data["summary"])
    if data["checkpoints"]:
        table = Table(title="Checkpoints", title_justify="left", header_style="bold")
        for column in ("Nom", "Step", "Phase", "Tags", "Local"):
            table.add_column(column)
        for ckpt in data["checkpoints"]:
            table.add_row(escape(str(ckpt.get("name"))), str(ckpt.get("step")),
                          escape(str(ckpt.get("phase"))), escape(", ".join(ckpt.get("tags") or [])),
                          "oui" if ckpt.get("available_locally") else "non")
        console.print(table)
    if data["evals"]:
        table = Table(title="Évaluations", title_justify="left", header_style="bold")
        for column in ("Checkpoint", "Benchmark", "Résumé"):
            table.add_column(column)
        for ev in data["evals"]:
            summary = ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                                for k, v in (ev.get("summary") or {}).items())
            table.add_row(escape(str(ev.get("checkpoint"))), escape(str(ev.get("benchmark"))),
                          escape(summary))
        console.print(table)


# -- users (§12.2) --------------------------------------------------------------------------
def _users_file() -> Path:
    from wmlab.paths import data_root
    from wmlab.users import users_path

    try:
        return users_path(data_root())
    except Exception as exc:
        _fail(str(exc))
    raise AssertionError  # unreachable


@users_app.command("add", help="Créer un utilisateur et afficher son jeton (une seule fois).")
def users_add(
    name: str = typer.Argument(..., help="Nom de l'utilisateur (lettres, chiffres, '_', '-', '.')."),
    role: str = typer.Option("member", "--role", help="admin ou member."),
) -> None:
    from wmlab.users import UserError, add_user

    path = _users_file()
    try:
        user, token = add_user(name, role, path)
    except UserError as exc:
        _fail(str(exc))
        return
    console = _out()
    console.print(f"[green]Utilisateur créé :[/green] {escape(user.name)} ({escape(user.role)})")
    console.print(f"Jeton (affiché une seule fois, à transmettre à l'utilisateur) : "
                  f"[bold]{escape(token)}[/bold]")
    console.print(f"[dim]Fichier : {escape(str(path))}[/dim]")


@users_app.command("list", help="Lister les utilisateurs (actifs et révoqués).")
def users_list(as_json: bool = typer.Option(False, "--json", help="Sortie JSON.")) -> None:
    from wmlab.users import load_users

    users = load_users(_users_file())
    if as_json:
        _echo_json([u.to_api() for u in users])
        return
    console = _out()
    if not users:
        console.print("[dim]Aucun utilisateur (wmlab users add NAME --role admin|member).[/dim]")
        return
    table = Table(header_style="bold")
    for column in ("Nom", "Rôle", "Créé le", "Révoqué le"):
        table.add_column(column)
    for user in users:
        style = "dim" if user.revoked_at else ""
        table.add_row(escape(user.name), escape(user.role), escape(user.created_at),
                      escape(user.revoked_at or "—"), style=style)
    console.print(table)


@users_app.command("revoke", help="Révoquer le jeton d'un utilisateur.")
def users_revoke(name: str = typer.Argument(..., help="Nom de l'utilisateur.")) -> None:
    from wmlab.users import UserError, revoke_user

    try:
        user = revoke_user(name, _users_file())
    except UserError as exc:
        _fail(str(exc))
        return
    _out().print(f"[yellow]Jeton révoqué :[/yellow] {escape(user.name)} ({escape(user.revoked_at or '')})")


# -- official pretrained weights ------------------------------------------------------------
@weights_app.command("list", help="Lister les poids officiels déclarés et leur présence locale.")
def weights_list(as_json: bool = typer.Option(False, "--json", help="Sortie JSON.")) -> None:
    from wmlab.weights import describe_weights

    rows = _run_delegated(describe_weights)
    if as_json:
        _echo_json(rows)
        return
    table = Table(title=f"Poids officiels ({len(rows)})", title_justify="left",
                  header_style="bold")
    for column in ("Nom", "Taille", "Présent", "Licence", "Description"):
        table.add_column(column)
    for row in rows:
        license_ = row["license"] + ("" if row["commercial_use"] else " [red](non commercial)[/red]")
        table.add_row(escape(row["name"]), f"~{row['size_mb']:g} Mo",
                      "[green]oui[/green]" if row["available"] else "non",
                      license_, escape(row["description"]))
    _out().print(table)


@weights_app.command("pull", help="Télécharger des poids officiels (source et licence affichées).")
def weights_pull(
    names: list[str] = typer.Argument(..., help="Noms des poids (voir « wmlab weights list »)."),
    force: bool = typer.Option(False, "--force", help="Retélécharger même si présents."),
) -> None:
    from wmlab.weights import pull_weights

    for name in names:
        _run_delegated(pull_weights, name, force=force)


def main() -> None:
    """Entry point of the ``wmlab`` script."""
    app()


if __name__ == "__main__":
    main()
