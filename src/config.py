"""Config loading + normalization."""
from __future__ import annotations

import os
import re
from datetime import datetime, time, date
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("PLANNER_CONFIG_DIR") or ROOT / "config")
PERSONAL = ("courses.yaml", "commitments.yaml")
DATA_DIR = Path(os.environ.get("PLANNER_DATA_DIR") or ROOT / "data")
OUT_DIR = Path(os.environ.get("PLANNER_OUT_DIR") or ROOT / "out")

DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class SetupNeeded(Exception):
    """The planner has not been pointed at anyone's classes yet."""


def _load(name: str, optional: bool = False) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        if optional:
            return {}
        if name in PERSONAL:
            raise SetupNeeded(f"{path} does not exist yet. Run `./plan setup` to "
                              f"connect your classes (it writes this file for you).")
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _merge(base: dict, over: dict) -> dict:
    """`over` on top of `base`, key by key; nested maps merge, the rest replace."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value):
    """Expand ${VAR} in config strings."""
    if not isinstance(value, str):
        return value
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file and rename, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666 if mode is None else mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_prefs() -> dict:
    """prefs.yaml is shared and changes with the code."""
    return _merge(_load("prefs.yaml"), _load("prefs.local.yaml", optional=True))


class Config:
    def __init__(self):
        self.commitments = _load("commitments.yaml")
        self.courses_raw = _load("courses.yaml")
        self.prefs = load_prefs()

        self.tz = ZoneInfo(self.commitments.get("timezone", "America/Denver"))
        self.courses = {c["key"]: c for c in self.courses_raw.get("courses", [])}
        self.course_defaults = self.courses_raw.get("defaults", {})

        self.canvas_map = {
            int(c["canvas_id"]): c["key"]
            for c in self.courses.values()
            if c.get("canvas_id")
        }

        self._load_env()

    def _load_env(self):
        """Read .env if present; real env vars win."""
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    def env(self, key: str, default: str = "") -> str:
        return os.environ.get(key, default)

    def expand(self, value):
        return expand_env(value)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def at(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self.tz)

    def term_bounds(self) -> tuple[date, date]:
        t = self.commitments.get("term", {})
        return t.get("start"), t.get("end")

    def course(self, key: str) -> dict:
        return self.courses.get(key, {})

    def ls_course_id(self, key: str) -> str:
        c = self.course(key)
        cid = c.get("ls_course_id")
        if not cid:
            url = self.expand(c.get("ls_ics_url") or "")
            if "courseID=" in url:
                cid = url.split("courseID=")[1].split("&")[0]
        return cid or ""

    def ls_course_ids(self) -> dict[str, str]:
        """course key -> LearningSuite id, for every course that has one."""
        return {k: cid for k in self.courses if (cid := self.ls_course_id(k))}

    def ls_url(self, key: str, page: str = "gradebook") -> str:
        cid = self.ls_course_id(key)
        return (f"https://learningsuite.byu.edu/.x/cid-{cid}/student/{page}"
                if cid else "")

    def weight_for(self, course_key: str, category: str) -> float:
        """Fraction of the final grade this category is worth."""
        c = self.course(course_key)
        w = c.get("weights") or self.course_defaults.get("weights", {})
        cat = (category or "").lower()
        if cat in w:
            return float(w[cat])
        for k, v in w.items():
            if k.lower() in cat or cat in k.lower():
                return float(v)
        listed = sum(float(x) for x in w.values())
        residual = max(0.0, 1.0 - listed)
        return float(self.course_defaults.get("unlisted_category_weight",
                                              min(residual, 0.05)))

    def effort_for(self, course_key: str, category: str) -> int:
        c = self.course(course_key)
        e = c.get("effort") or self.course_defaults.get("effort", {})
        cat = (category or "").lower()
        if cat in e:
            return int(e[cat])
        for k, v in e.items():
            if k.lower() in cat or cat in k.lower():
                return int(v)
        return int(self.course_defaults.get("effort", {}).get("homework", 120))

    def exam_ramp_days(self, course_key: str) -> int:
        c = self.course(course_key)
        return int(c.get("exam_ramp_days") or self.course_defaults.get("exam_ramp_days", 7))

    def travel_minutes(self, a: str, b: str) -> int:
        if not a or not b or a == b:
            return 0
        tr = self.commitments.get("travel", {})
        for row in tr.get("matrix", []):
            if {row["from"], row["to"]} == {a, b}:
                return int(row["minutes"])
        return int(tr.get("default_minutes", 15))
