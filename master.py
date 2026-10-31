"""The master document: one JSON file per user, data/master/<user_id>.json,
written only by the user (through the Profile tab, or by editing the file).
No LLM touches it.

    {"profile": {"name": ..., "title": ..., ...},
     "skills":  [{"name": ..., "category": ..., "active": true}],
     "points":  [{"id": 7, "entry_type": "role"|"project", "company": ..., "role": ...,
                  "start_date": ..., "end_date": null, "text": ..., "tags": [...], "active": true}],
     "next_point_id": 8}

A point's id is stable for the life of the file: analysis results refer to it.
Every write goes to a temp file and is renamed over the old one, so a reader
(the worker) sees the whole old document or the whole new one, never half. The
lock only keeps two edits from different browser requests from overwriting
each other; the worker never takes it.
"""

import json
import os
import tempfile
import threading

import db

_write_lock = threading.Lock()
ENTRY_TYPES = ("role", "project")


def _dir():
    return os.path.join(db.DATA_DIR, "master")


def path(user_id):
    return os.path.join(_dir(), f"{int(user_id)}.json")


def _empty():
    return {"profile": {}, "skills": [], "points": [], "next_point_id": 1}


def validate(doc):
    """Raises ValueError saying exactly what is wrong; returns the document.
    A hand-edited file that breaks the shape should fail loudly here, not
    deep inside a resume render."""
    if not isinstance(doc, dict):
        raise ValueError("master document must be a JSON object")
    if not isinstance(doc.get("profile"), dict) or not all(isinstance(v, str) for v in doc["profile"].values()):
        raise ValueError("profile must be an object of text values")
    names = set()
    for s in doc.get("skills", []):
        if not isinstance(s, dict) or not str(s.get("name", "")).strip() or not str(s.get("category", "")).strip():
            raise ValueError(f"every skill needs a name and a category: {s!r}")
        if s["name"].lower() in names:
            raise ValueError(f"skill listed twice: {s['name']}")
        names.add(s["name"].lower())
    ids = set()
    for p in doc.get("points", []):
        if not isinstance(p, dict) or not isinstance(p.get("id"), int):
            raise ValueError(f"every point needs an integer id: {p!r}")
        if p["id"] in ids:
            raise ValueError(f"point id {p['id']} used twice")
        ids.add(p["id"])
        if p.get("entry_type") not in ENTRY_TYPES:
            raise ValueError(f"point {p['id']}: entry_type must be one of {ENTRY_TYPES}")
        for key in ("company", "role", "text"):
            if not str(p.get(key, "")).strip():
                raise ValueError(f"point {p['id']}: {key} is empty")
        if not isinstance(p.get("tags", []), list):
            raise ValueError(f"point {p['id']}: tags must be a list")
    if ids and doc.get("next_point_id", 0) <= max(ids):
        raise ValueError("next_point_id must be larger than every point id")
    return doc


def load(user_id):
    """The user's document; an empty one if they have none yet."""
    try:
        with open(path(user_id)) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return _empty()
    except ValueError as e:
        raise ValueError(f"{path(user_id)} is not valid JSON: {e}")
    doc.setdefault("skills", [])
    doc.setdefault("points", [])
    doc.setdefault("next_point_id", max([p["id"] for p in doc["points"] if isinstance(p.get("id"), int)], default=0) + 1)
    return validate(doc)


def save(user_id, doc):
    """Validate, then replace the file atomically."""
    validate(doc)
    os.makedirs(_dir(), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_dir(), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path(user_id))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def delete(user_id):
    """Remove a user's document (when the user is removed)."""
    try:
        os.unlink(path(user_id))
    except FileNotFoundError:
        pass


def _edit(user_id, fn):
    with _write_lock:
        doc = load(user_id)
        result = fn(doc)
        save(user_id, doc)
        return result


# ── profile ─────────────────────────────────────────────────────────────
def profile_all(user_id):
    return dict(load(user_id)["profile"])


def profile_set(user_id, key, value):
    key = (key or "").strip()
    if not key:
        raise ValueError("profile key is empty")
    _edit(user_id, lambda d: d["profile"].__setitem__(key, str(value)))


# ── skills ──────────────────────────────────────────────────────────────
def skills_all(user_id, active_only=True):
    skills = [s for s in load(user_id)["skills"] if s.get("active", True) or not active_only]
    return sorted(({"name": s["name"], "category": s["category"], "active": s.get("active", True)} for s in skills),
                  key=lambda s: (s["category"].lower(), s["name"].lower()))


def skills_add(user_id, name, category):
    """Case-insensitive: re-adding with different casing updates the one
    existing skill (keeping its spelling) and makes it active again."""
    name, category = (name or "").strip(), (category or "").strip() or "Other"
    if not name:
        raise ValueError("skill name is empty")

    def add(doc):
        for s in doc["skills"]:
            if s["name"].lower() == name.lower():
                s["category"], s["active"] = category, True
                return
        doc["skills"].append({"name": name, "category": category, "active": True})
    _edit(user_id, add)


# ── points ──────────────────────────────────────────────────────────────
def point_bank_all(user_id, active_only=True):
    """Newest start first, then in the order they were added."""
    points = [dict(p, tags=sorted(p.get("tags", [])), active=bool(p.get("active", True)),
                   end_date=p.get("end_date") or None, start_date=p.get("start_date", ""))
              for p in load(user_id)["points"]]
    if active_only:
        points = [p for p in points if p["active"]]
    points.sort(key=lambda p: p["id"])
    points.sort(key=lambda p: p["start_date"], reverse=True)     # stable: ids stay ascending within a date
    return points


def point_bank_add(user_id, company, role, start_date, end_date, text, tags=(), entry_type="role"):
    """Returns the new point's id."""
    def add(doc):
        pid = doc["next_point_id"]
        doc["points"].append({
            "id": pid, "entry_type": entry_type, "company": (company or "").strip(), "role": (role or "").strip(),
            "start_date": (start_date or "").strip(), "end_date": (end_date or "").strip() or None,
            "text": (text or "").strip(), "tags": sorted({t.strip() for t in tags if t and t.strip()}), "active": True})
        doc["next_point_id"] = pid + 1
        return pid
    return _edit(user_id, add)


def _point(doc, pid):
    for p in doc["points"]:
        if p["id"] == pid:
            return p
    raise ValueError(f"no point with id={pid}")


def point_bank_update_text(user_id, pid, text):
    if not (text or "").strip():
        raise ValueError("point text is empty")
    _edit(user_id, lambda d: _point(d, pid).__setitem__("text", text.strip()))


def point_bank_set_active(user_id, pid, active):
    _edit(user_id, lambda d: _point(d, pid).__setitem__("active", bool(active)))

