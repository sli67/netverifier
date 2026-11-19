"""Loads a snapshot into Batfish, reusing an already parsed model when one exists."""

import json
import logging
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from pybatfish.client.session import Session

NETWORK = "verifier"
CONFIGS_PREFIX = "configs/"

logging.getLogger("pybatfish").setLevel(logging.WARNING)


def server_version(host: str) -> str:
    with urllib.request.urlopen(f"http://{host}:9996/v2/version", timeout=10) as response:
        return json.load(response)["Batfish"]


def model_name(content_hash: str, version: str) -> str:
    """Name of the parsed model for this exact content under this Batfish version."""
    return f"{content_hash}_{re.sub(r'[^A-Za-z0-9]', '-', version)}"


def _strip(path: str) -> str:
    return path[len(CONFIGS_PREFIX) :] if path.startswith(CONFIGS_PREFIX) else path


def _clean(value: Any) -> Any:
    """Convert a pandas cell to something JSON can hold."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if value != value else value  # NaN
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    return str(value)


def _report(bf: Session) -> dict[str, Any]:
    files = [
        {
            "path": _strip(row["File_Name"]),
            "status": row["Status"],
            "format": row["File_Format"],
            "nodes": _clean(row["Nodes"]) or [],
        }
        for _, row in bf.q.fileParseStatus().answer().frame().iterrows()
    ]

    issues = []
    for _, row in bf.q.initIssues().answer().frame().iterrows():
        sources = [
            {"path": _strip(source.filename), "lines": _clean(source.lines) or []}
            for source in (row["Source_Lines"] or [])
        ]
        issues.append(
            {
                "type": row["Type"],
                "details": row["Details"],
                "lineText": _clean(row["Line_Text"]),
                "nodes": _clean(row["Nodes"]) or [],
                "sources": sources,
            }
        )
    return {"files": sorted(files, key=lambda f: f["path"]), "issues": issues}


def dataplane_status(bf: Session) -> dict[str, Any]:
    """Whether Batfish can compute routing and forwarding for the current model.

    Parsing can succeed while this fails, for example when Batfish hits a bug in
    one protocol's simulation. Questions about routes, sessions and reachability
    then cannot run; questions about policies and filters still can.
    """
    try:
        # A routing question over no devices: cheap once the data plane exists, and computes it if not.
        bf.q.routes(nodes="/^$/").answer()
        return {"ok": True, "error": None}
    except Exception:  # noqa: BLE001 - the retry below is what reports the cause
        pass
    try:
        bf.generate_dataplane()
        return {"ok": True, "error": None}
    except Exception as error:  # noqa: BLE001
        text = str(error)
        # The server's reply is a JSON-escaped Java stack trace. Keep the root cause and where it arose.
        causes = re.findall(r'Caused by: (.*?)",\s*"\s*at ([\w.$]+)', text)
        if not causes:
            return {"ok": False, "error": text[:400]}
        message, frame = causes[-1]
        where = ".".join(frame.split(".")[-2:])
        return {"ok": False, "error": f"{message.replace(chr(92) + chr(34), chr(34))} (in {where})"[:400]}


def ensure_model(host: str, content_hash: str, fetch_snapshot: Callable[[], Path]) -> tuple[Session, bool, str]:
    """Return a session set to the parsed model of this content, parsing it first if needed.

    fetch_snapshot is only called when Batfish does not hold the model yet.
    Returns (session, reused, batfish_version).
    """
    version = server_version(host)
    name = model_name(content_hash, version)

    bf = Session(host=host)
    bf.set_network(NETWORK)
    reused = name in bf.list_snapshots()
    if reused:
        bf.set_snapshot(name)
    else:
        bf.init_snapshot(str(fetch_snapshot()), name=name, overwrite=True)
    return bf, reused, version


def load_candidate(bf: Session, base: str, name: str, files: dict[str, str]) -> dict[str, Any]:
    """Parse a candidate's files as their own model, and return its parse report.

    The session is left set to `base`, the model of the current configs.
    """
    try:
        if name in bf.list_snapshots():
            bf.set_snapshot(name)
        else:
            with tempfile.TemporaryDirectory(prefix="verifier-candidate-") as tmp:
                configs = (Path(tmp) / "configs").resolve()
                for path, text in files.items():
                    target = (configs / path).resolve()
                    if not target.is_relative_to(configs):
                        raise ValueError(f"unsafe path in candidate: {path!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
                bf.init_snapshot(tmp, name=name, overwrite=True)
        return _report(bf)
    finally:
        bf.set_snapshot(base)


def analyse(
    host: str,
    instance: str,
    content_hash: str,
    fetch_snapshot: Callable[[], Path],
) -> dict[str, Any]:
    """Make sure Batfish holds a parsed model of the snapshot and report on it.

    Returns the fields to store on the investigation.
    """
    started = time.monotonic()
    bf, reused, version = ensure_model(host, content_hash, fetch_snapshot)
    report = _report(bf)
    report["dataplane"] = dataplane_status(bf)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    recognised = [f for f in report["files"] if f["nodes"]]
    error = None
    if not recognised:
        error = "Batfish did not recognise any file in this snapshot as a supported device configuration."

    return {
        "status": "failed" if error else "ready",
        "error": error,
        "model_reused": reused,
        "batfish_instance": instance,
        "batfish_version": version,
        "parse_ms": elapsed_ms,
        "result": report,
    }
