"""The setup guide: seven ordered steps, each with a live done/not-done state read from real configuration.

Everything here is cheap and read-only: saved settings, a few ``stat`` calls on the folders the libraries use, one
indexed count in Reprise's own database, in-memory job, GPU and webhook state. It never asks Plex anything and never
opens Plex's database: the Plex step only looks at the files around it.

Steps 1 to 3 are required (Plex connected, libraries chosen with their folders visible, the library refreshed); 4 to 7
are optional. Each step comes back as plain words: a ``state``, one ``reason`` sentence, optional ``details`` lines,
optional copyable ``values`` and one ``action`` that opens the existing place where it is done.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

from loguru import logger

from ..config.paths import detect_unhealthy_media_mounts
from ..markers.fs import filesystem_type, is_network_filesystem
from ..markers.publishers.plex_db import plex_db_path, plex_machine_identifier
from ..servers.ownership import path_mapping_candidates
from . import acceleration

README = "https://github.com/nphil/reprise"
HELP_FOLDERS = {"label": "How to share media folders", "href": f"{README}#share-your-media-folders-with-reprise"}
HELP_GPU = {"label": "How to add a graphics card", "href": f"{README}#optional-gpu-decoding"}
HELP_SENDING = {"label": "Read the safe steps", "href": f"{README}#turn-on-sending-to-plex"}
CHANGE_IN_LIBRARY = {"label": "Change in Library", "href": "/reprise"}

# Where Plex's whole config folder is shared with Reprise when nothing else is saved (what the docs and the template use).
SUGGESTED_PLEX_FOLDER = "/plex"

STEP_TITLES = {
    "connect_plex": "Connect Plex",
    "libraries_and_folders": "Choose libraries and check your media folders",
    "refresh_library": "Refresh the library",
    "gpu": "Use a graphics card (GPU)",
    "online_lookup": "Look up intro times online",
    "webhooks": "Connect Sonarr and Radarr",
    "send_to_plex": "Send results to Plex",
}
REQUIRED_STEPS = ("connect_plex", "libraries_and_folders", "refresh_library")

# Why Plex's database can't be used, as one plain sentence about the folder (the caller names the server). The Sharing
# switch's refusal and the guide's step read from here, so they never disagree about what is missing.
_DATABASE_PROBLEMS = {
    "folder_unset": (
        "the Plex config folder isn't set in Reprise. Enter {suggested} under Connections, Edit, Plex config folder, "
        "and share Plex's whole “Plex Media Server” folder with Reprise at that path, read-write."
    ),
    "folder_missing": (
        "{folder} isn't there inside Reprise. Share Plex's whole “Plex Media Server” folder with Reprise at {folder}, "
        "read-write."
    ),
    "database_missing": (
        "{folder} is shared, but Plex's database isn't in it (Reprise looked for {database}). Only part of Plex's "
        "folder is shared, for example just Media/localhost. Share the whole “Plex Media Server” folder with Reprise, "
        "read-write."
    ),
    "identity_unreadable": (
        "Plex's Preferences.xml isn't in {folder}, so Reprise can't tell whose database this is. Share the whole "
        "“Plex Media Server” folder, not just a part of it."
    ),
    "identity_mismatch": (
        "the folder at {folder} belongs to a different Plex server than the one Reprise is connected to. Share the "
        "“Plex Media Server” folder of this Plex."
    ),
    "read_only": (
        "Plex's database is there, but Reprise isn't allowed to write to it. Share the folder read-write (without "
        "the read-only option) and run Reprise as the user that owns Plex's files (on Unraid that is usually PUID 99 "
        "and PGID 100)."
    ),
    "network_share": (
        "Plex's database is on a network share ({filesystem}). Reprise has to run on the same machine as Plex and see "
        "its folder directly, because SQLite can't share its file locks over a network."
    ),
}

_GPU_STATE_WORDS = {
    "ready": "ready",
    "off": "turned off",
    "checking": "being tested",
    "unavailable": "can't decode your video exactly as the processor does, so it isn't used",
    "paused": "paused for ten minutes after repeated errors",
}


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else many or one + 's'}"


def _names(names: Iterable[str]) -> str:
    """``A``, ``A and B`` or ``A, B and C``."""
    items = [name for name in names if name]
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _step(
    step_id: str,
    state: str,
    reason: str,
    *,
    details: Iterable[str] = (),
    values: Iterable[dict[str, str]] = (),
    action: dict[str, str] | None = None,
    help_link: dict[str, str] | None = None,
    progress: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "number": list(STEP_TITLES).index(step_id) + 1,
        "title": STEP_TITLES[step_id],
        "required": step_id in REQUIRED_STEPS,
        "state": state,
        "done": state == "done",
        "reason": reason,
        "details": list(details),
        "values": [dict(value) for value in values],
        "action": dict(action) if action else None,
        "help": dict(help_link) if help_link else None,
        "progress": progress,
        "data": data or {},
    }


def _guarded(step_id: str, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """One step's answer; a probe that fails becomes a "could not check" step instead of breaking the whole page."""
    try:
        return build()
    except Exception:
        logger.exception("Setup guide: could not check the step {!r}", step_id)
        return _step(step_id, "todo", "Reprise couldn't check this just now. Open the guide again in a moment.")


def _plex_servers(configs: Iterable[Any]) -> list[Any]:
    return [cfg for cfg in configs if getattr(cfg.type, "value", cfg.type) == "plex"]


def _usable(cfg: Any) -> bool:
    """A Plex entry that is switched on and has an address and a saved sign-in."""
    return bool(cfg.enabled and str(cfg.url or "").strip() and str((cfg.auth or {}).get("token") or "").strip())


def _connect_plex(plex: list[Any], wizard_complete: bool) -> dict[str, Any]:
    usable = [cfg for cfg in plex if _usable(cfg)]
    if usable:
        servers = [{"id": cfg.id, "name": cfg.name or "Plex"} for cfg in usable]
        return _step(
            "connect_plex",
            "done",
            f"Connected to {_names(server['name'] for server in servers)}.",
            data={"servers": servers},
        )
    if plex:
        name = plex[0].name or "Plex"
        if not plex[0].enabled:
            why = f"{name} is added but switched off. Switch it on in Connections."
        elif not str(plex[0].url or "").strip():
            why = f"{name} is added but has no address saved. Open Connections and check it."
        else:
            why = f"{name} is added, but Reprise isn't signed in to it any more. Open Connections and sign in again."
        return _step(
            "connect_plex",
            "todo",
            why,
            action={"label": "Open Connections", "href": "/servers"},
            data={"servers": []},
        )
    return _step(
        "connect_plex",
        "todo",
        "Reprise isn't connected to a Plex server yet. You sign in with your Plex account; it takes about a minute.",
        action={"label": "Connect Plex", "href": "/servers?add=plex" if wizard_complete else "/setup"},
        data={"servers": []},
    )


def _folder_state(path: str) -> str:
    """``ok``, ``missing``, ``empty`` or ``unreadable``: the probes the media-mount banner uses, on one folder."""
    try:
        if not os.path.isdir(path):
            return "missing"
        with os.scandir(path) as entries:
            return "ok" if next(entries, None) is not None else "empty"
    except PermissionError:
        return "unreadable"
    except OSError:
        return "missing"


def _library_folders(cfg: Any, library: Any) -> list[dict[str, Any]]:
    """What Reprise can see of each folder Plex reports for one library, after the server's path mappings."""
    folders = []
    for plex_path in library.remote_paths:
        states = [
            (local, prefix, _folder_state(local))
            for local, prefix in path_mapping_candidates(plex_path, cfg.path_mappings)
        ]
        local, prefix, issue = next((s for s in states if s[2] == "ok"), states[0])
        mount_issue = None
        if issue != "ok" and prefix is not None:
            mount_issue = next((i["issue"] for i in detect_unhealthy_media_mounts([{"local_prefix": prefix}])), None)
        folders.append(
            {
                "server_id": cfg.id,
                "server_name": cfg.name or "Plex",
                "library": library.name,
                "plex_path": plex_path,
                "container_path": local,
                "mount": prefix or plex_path,
                "mapped": prefix is not None,
                "issue": None if issue == "ok" else issue,
                "mount_issue": mount_issue,
            }
        )
    return folders


def _folder_sentence(folder: dict[str, Any]) -> str:
    """One missing folder, in plain words: what Plex calls it, where Reprise looks, and which mount to add."""
    where, plex_path, local, mount = folder["library"], folder["plex_path"], folder["container_path"], folder["mount"]
    if folder["issue"] == "empty":
        return (
            f"{where}: {local} is there but empty. If your files are on a network share, it may not have been ready "
            "when Reprise started. Restart Reprise once the share is up."
        )
    if folder["issue"] == "unreadable":
        return (
            f"{where}: Reprise can't read {local}. Check the share's permissions for the user Reprise runs as "
            "(PUID and PGID)."
        )
    if not folder["mapped"]:
        return (
            f"{where}: Plex calls this folder {plex_path}. Reprise looks for the same path and finds nothing there. "
            f"Share it with Reprise at {plex_path}, read-only, or add a path mapping to where you did share it "
            "(Connections, Edit, Path mappings)."
        )
    if folder["mount_issue"] is None:
        return (
            f"{where}: Reprise looks for {plex_path} at {local}. {mount} is shared, but that folder isn't inside it. "
            "Check the folder name and the path mapping (Connections, Edit, Path mappings)."
        )
    return (
        f"{where}: Plex calls this folder {plex_path}. Reprise looks for it at {local} and finds nothing there. "
        f"Share the folder that holds it with Reprise at {mount}, read-only."
    )


def _libraries_and_folders(usable: list[Any]) -> dict[str, Any]:
    chosen = [(cfg, library) for cfg in usable for library in cfg.libraries if library.enabled]
    if not chosen:
        return _step(
            "libraries_and_folders",
            "todo",
            "No library is chosen yet. Pick the TV libraries Reprise should look at.",
            action={
                "label": "Choose libraries",
                "href": f"/servers?edit={quote(usable[0].id, safe='')}&tab=libraries",
            },
            data={"libraries": [], "folders": [], "problems": 0},
        )
    folders = [folder for cfg, library in chosen for folder in _library_folders(cfg, library)]
    problems = [folder for folder in folders if folder["issue"]]
    names = [library.name for _cfg, library in chosen]
    shown = _names(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    data = {"libraries": names, "folders": folders, "problems": len(problems)}
    if not problems:
        seen = ""
        if len(folders) == 1:
            seen = " Reprise can see the folder."
        elif folders:
            seen = f" Reprise can see all {len(folders)} of their folders."
        label = _plural(len(chosen), "library", "libraries")
        return _step("libraries_and_folders", "done", f"{label} chosen: {shown}.{seen}", data=data)
    total, missing = len(folders), len(problems)
    if total == 1:
        lead = "Reprise can't see the folder your library uses."
    elif missing == total:
        lead = f"Reprise can't see any of the {total} folders your libraries use."
    else:
        lead = f"Reprise can't see {missing} of the {total} folders your libraries use."
    shown_problems = [_folder_sentence(folder) for folder in problems[:6]]
    if len(problems) > 6:
        shown_problems.append(f"And {len(problems) - 6} more.")
    server_id = quote(problems[0]["server_id"], safe="")
    return _step(
        "libraries_and_folders",
        "todo",
        f"{lead} It only reads your files, so every folder Plex uses has to be shared with it.",
        details=shown_problems,
        action={"label": "Fix media folders", "href": f"/servers?edit={server_id}&tab=paths"},
        help_link=HELP_FOLDERS,
        data=data,
    )


def _active_refresh_percent(job_manager: Any) -> float | None:
    """The progress of a library refresh that is queued or running now, or None when there is none."""
    if job_manager is None:
        return None
    for job in job_manager.get_all_jobs():
        if (
            job.kind == "reprise"
            and (job.config or {}).get("refresh_request")
            and job.status.value in ("pending", "running")
        ):
            return float(getattr(job.progress, "percent", 0.0) or 0.0)
    return None


def _refresh_library(service: Any, earlier_done: bool) -> dict[str, Any]:
    total = service.store.media_file_count()
    if total:
        return _step(
            "refresh_library",
            "done",
            f"Reprise has read {_plural(total, 'episode file')} from Plex.",
            data={"episode_files": total},
        )
    percent = _active_refresh_percent(service.job_manager)
    if percent is not None:
        return _step(
            "refresh_library",
            "working",
            "Reading your library from Plex now. This can take a few minutes.",
            progress={"percent": round(percent)},
            data={"episode_files": 0},
        )
    if not earlier_done:
        return _step("refresh_library", "blocked", "Finish the steps above first.", data={"episode_files": 0})
    return _step(
        "refresh_library",
        "todo",
        "Reprise hasn't read your episodes yet. A refresh reads them from Plex. It doesn't analyze anything and it "
        "doesn't change Plex or your files.",
        action={"label": "Refresh library", "href": "/reprise?refresh=1"},
        data={"episode_files": 0},
    )


def required_steps(service: Any, wizard_complete: bool) -> list[dict[str, Any]]:
    """Steps 1 to 3, each guarded: the only ones the home page needs to decide where to send the owner."""
    plex = _plex_servers(service.registry.configs())
    connect = _guarded("connect_plex", lambda: _connect_plex(plex, wizard_complete))
    if connect["done"]:
        usable = [cfg for cfg in plex if _usable(cfg)]
        folders = _guarded("libraries_and_folders", lambda: _libraries_and_folders(usable))
    else:
        folders = _step(
            "libraries_and_folders",
            "blocked",
            "Connect Plex first.",
            data={"libraries": [], "folders": [], "problems": 0},
        )
    refresh = _guarded("refresh_library", lambda: _refresh_library(service, connect["done"] and folders["done"]))
    return [connect, folders, refresh]


def required_open(service: Any, wizard_complete: bool) -> bool:
    """Whether some required step is not done (what the home page asks, without touching the optional steps)."""
    return not all(step["done"] for step in required_steps(service, wizard_complete))


def _gpu() -> dict[str, Any]:
    status = acceleration.status()
    devices = status.get("devices") or []
    details = [
        f"{device.get('name') or 'Graphics card'} ({device.get('method')} decode): "
        f"{_GPU_STATE_WORDS.get(device.get('state'), device.get('state'))}"
        for device in devices
    ]
    data = {"label": status.get("label"), "enabled": bool(status.get("enabled")), "devices": devices}
    if status.get("active"):
        return _step(
            "gpu", "done", f"Analysis uses {status['label']}.", details=details, action=CHANGE_IN_LIBRARY, data=data
        )
    if not devices:
        return _step(
            "gpu",
            "todo",
            "No graphics card was found, so analysis uses your processor (CPU). That works fine; a graphics card only "
            "makes the video decoding a little faster.",
            help_link=HELP_GPU,
            data=data,
        )
    if not status.get("enabled"):
        return _step(
            "gpu",
            "todo",
            "GPU decoding is turned off, so analysis uses your processor (CPU). You can turn it on in the Library.",
            details=details,
            action=CHANGE_IN_LIBRARY,
            data=data,
        )
    if any(device.get("state") == "checking" for device in devices):
        return _step(
            "gpu",
            "working",
            "Reprise found a graphics card and is testing it now. This takes about a minute.",
            details=details,
            action=CHANGE_IN_LIBRARY,
            data=data,
        )
    return _step(
        "gpu",
        "todo",
        "Reprise found a graphics card but can't use it, so analysis uses your processor (CPU).",
        details=details,
        action=CHANGE_IN_LIBRARY,
        help_link=HELP_GPU,
        data=data,
    )


def _online_lookup(service: Any) -> dict[str, Any]:
    if service.online_lookups_enabled():
        return _step(
            "online_lookup",
            "done",
            "On, which is the default. Reprise asks the public databases IntroDB and SkipDB where intros and credits "
            "are. It sends only the show's id, the season and episode numbers and the file's length. Their answers "
            "are only suggestions: nothing is skipped until you approve it.",
            action=CHANGE_IN_LIBRARY,
            data={"enabled": True},
        )
    return _step(
        "online_lookup",
        "todo",
        "Off. Reprise only compares your own episodes. Turn it on if you want extra hints from IntroDB and SkipDB; "
        "their answers are only suggestions, and nothing is skipped until you approve it.",
        action=CHANGE_IN_LIBRARY,
        data={"enabled": False},
    )


def _webhooks(settings: Any, base_url: str, seen: set[str]) -> dict[str, Any]:
    urls = {name: f"{base_url}/api/webhooks/{name}" for name in ("sonarr", "radarr")}
    values = [
        {"label": "Sonarr address", "value": urls["sonarr"]},
        {"label": "Radarr address", "value": urls["radarr"]},
    ]
    enabled = settings.get("webhook_enabled", True) is not False
    contacted = [name for name in ("Sonarr", "Radarr") if name.lower() in seen]
    details = [
        "To connect them, paste the address into Sonarr or Radarr (Settings, Connect, Webhook) and put your webhook "
        "secret in the Password field. You find the secret under Settings, then Connections, then Webhook secret.",
        "Today these webhooks only drive Plex preview thumbnails, not intro and credits analysis. For review only, "
        "save and test the connection but leave its import and upgrade events off.",
    ]
    if not enabled:
        details.append("Webhooks are switched off under Automation, so Reprise ignores anything they send.")
    data = {"urls": urls, "contacted": [name.lower() for name in contacted], "enabled": enabled}
    action = {"label": "Open Automation", "href": "/automation#section-webhooks-sonarr-radarr"}
    if contacted:
        verb = "has" if len(contacted) == 1 else "have"
        return _step(
            "webhooks",
            "done",
            f"{_names(contacted)} {verb} already contacted Reprise, so the connection works.",
            details=details,
            values=values,
            action=action,
            data=data,
        )
    return _step(
        "webhooks",
        "todo",
        "Not connected. Skip this if you don't use Sonarr or Radarr: after new episodes arrive, press Refresh "
        "library instead.",
        details=details,
        values=values,
        action=action,
        data=data,
    )


def plex_database_findings(config: Any) -> dict[str, Any]:
    """Whether Plex's own database is visible to Reprise for one Plex server, and if not, exactly what is missing.

    The first three checks (config folder set, folder there, database file in it) are what the Sharing switch looks at
    before it tries an episode. The rest are what that episode check would later refuse for, read from the same files
    without opening the database: Plex's Preferences.xml, write access, a network share.

    Args:
        config: One Plex server's ``ServerConfig``.

    Returns:
        ``server_id``, ``server_name``, ``config_folder``, ``database_path``, ``database_found`` (the switch's test),
        ``problem`` (None when nothing is wrong, else a key of ``_DATABASE_PROBLEMS``) and ``message``, the plain
        sentence for that problem ("" when there is none).
    """
    folder = str((config.output or {}).get("plex_config_folder") or "").strip()
    database = plex_db_path(folder) if folder else ""
    found = bool(database) and os.path.isfile(database)
    filesystem = ""
    problem: str | None = None
    if not folder:
        problem = "folder_unset"
    elif not os.path.isdir(folder):
        problem = "folder_missing"
    elif not found:
        problem = "database_missing"
    else:
        identity = plex_machine_identifier(folder)
        if not identity:
            problem = "identity_unreadable"
        elif config.server_identity and identity != config.server_identity:
            problem = "identity_mismatch"
        else:
            filesystem = filesystem_type(os.path.realpath(database)) or ""
            files = [path for path in (database, f"{database}-wal", f"{database}-shm") if os.path.exists(path)]
            if is_network_filesystem(filesystem):
                problem = "network_share"
            elif not all(os.access(path, os.W_OK) for path in files) or not os.access(
                os.path.dirname(database), os.W_OK | os.X_OK
            ):
                problem = "read_only"
    message = (
        _DATABASE_PROBLEMS[problem].format(
            suggested=SUGGESTED_PLEX_FOLDER, folder=folder, database=database, filesystem=filesystem
        )
        if problem
        else ""
    )
    return {
        "server_id": config.id,
        "server_name": config.name or "Plex",
        "config_folder": folder,
        "database_path": database or None,
        "database_found": found,
        "problem": problem,
        "message": message,
    }


def sending_refusal(findings: list[dict[str, Any]]) -> str:
    """The Sharing switch's refusal when no Plex database file is visible: the first server's specific problem."""
    why = findings[0]["message"] if findings and findings[0]["message"] else "no Plex server is connected."
    return f"Sending to Plex needs Plex's database, and Reprise can't see it: {why}"


def _send_to_plex(service: Any, usable: list[Any]) -> dict[str, Any]:
    switch = {"label": "Open Sharing", "href": "/reprise/sharing"}
    if getattr(service.publisher, "writes_enabled", False):
        return _step(
            "send_to_plex",
            "done",
            "Sending to Plex is on. Every change still needs your confirmation.",
            action=switch,
            data={"switch_on": True},
        )
    if not usable:
        return _step(
            "send_to_plex",
            "blocked",
            "Connect Plex first.",
            help_link=HELP_SENDING,
            data={"switch_on": False, "servers": []},
        )
    findings = [plex_database_findings(cfg) for cfg in usable]
    data = {"switch_on": False, "servers": findings}
    lead = "Review only is the default, so Reprise changes nothing in Plex."
    broken = [finding for finding in findings if finding["problem"]]
    if not broken:
        return _step(
            "send_to_plex",
            "todo",
            f"{lead} Reprise can see Plex's database, so you can turn sending on in Sharing whenever you are ready. "
            "Back up Plex's database first.",
            action=switch,
            help_link=HELP_SENDING,
            data=data,
        )
    folder = broken[0]["config_folder"] or SUGGESTED_PLEX_FOLDER
    return _step(
        "send_to_plex",
        "todo",
        f"{lead} To turn sending on, Reprise has to see Plex's own database, and it can't yet.",
        details=[f"{finding['server_name']}: {finding['message']}" for finding in broken],
        values=[
            {"label": "Share Plex's whole config folder at", "value": folder},
            {"label": "Environment variable", "value": f"PLEX_CONFIG_FOLDER={folder}"},
        ],
        action=switch,
        help_link=HELP_SENDING,
        data=data,
    )


def build_status(
    service: Any,
    settings: Any,
    *,
    base_url: str,
    guide_state: str,
    wizard_complete: bool,
    seen_webhooks: set[str] | None = None,
) -> dict[str, Any]:
    """Every step's state and reason, plus whether the home page should open the guide by itself.

    Args:
        service: The ``RepriseService`` (its registry, store, job manager and publisher).
        settings: The settings manager.
        base_url: The address the owner opened Reprise at, no trailing slash (for the webhook addresses).
        guide_state: The owner's saved choice: ``pending``, ``dismissed`` or ``completed``.
        wizard_complete: Whether the first-run wizard was finished or skipped.
        seen_webhooks: Which senders have contacted a webhook; read from the saved history when None.

    Returns:
        The payload of ``GET /api/reprise/setup-guide``.
    """
    from ..web.webhooks import webhook_sources_seen

    required = required_steps(service, wizard_complete)
    usable = [cfg for cfg in _plex_servers(service.registry.configs()) if _usable(cfg)]
    seen = webhook_sources_seen() if seen_webhooks is None else seen_webhooks
    optional = [
        _guarded("gpu", _gpu),
        _guarded("online_lookup", lambda: _online_lookup(service)),
        _guarded("webhooks", lambda: _webhooks(settings, base_url, seen)),
        _guarded("send_to_plex", lambda: _send_to_plex(service, usable)),
    ]
    done = sum(1 for step in required if step["done"])
    return {
        "state": guide_state,
        "auto_open": guide_state == "pending" and done < len(required),
        "required_total": len(required),
        "required_done": done,
        "all_required_done": done == len(required),
        "steps": required + optional,
    }
