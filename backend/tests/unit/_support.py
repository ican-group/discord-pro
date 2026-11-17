"""Shared helpers for offline unit tests: path setup and a minimal in-memory
async MongoDB stand-in (only the operations manage.py uses)."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _get(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _matches(doc, flt):
    for key, cond in (flt or {}).items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        present, value = _get(doc, key)
        if isinstance(cond, dict) and "$exists" in cond:
            if present != bool(cond["$exists"]):
                return False
        elif isinstance(cond, dict) and "$in" in cond:
            values = value if isinstance(value, list) else [value]
            if not present or not any(v in cond["$in"] for v in values):
                return False
        elif present and isinstance(value, list) and not isinstance(cond, (list, dict)):
            if cond not in value:  # MongoDB: scalar equality matches array elements
                return False
        elif not present or value != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, n):
        return [copy.deepcopy(d) for d in self._docs[:n]]


class FakeCollection:
    def __init__(self, name, log):
        self.name = name
        self.docs = []
        self.indexes = []
        self._log = log

    async def count_documents(self, flt, limit=0):
        n = sum(1 for d in self.docs if _matches(d, flt))
        return min(n, limit) if limit else n

    async def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if _matches(d, flt or {}):
                return copy.deepcopy(d)
        return None

    def find(self, flt=None, projection=None):
        return _Cursor([d for d in self.docs if _matches(d, flt or {})])

    async def insert_one(self, doc):
        self._log.append(("insert_one", self.name))
        self.docs.append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, flt, update):
        self._log.append(("update_one", self.name))
        for d in self.docs:
            if _matches(d, flt):
                _apply_set(d, update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def update_many(self, flt, update):
        self._log.append(("update_many", self.name))
        n = 0
        for d in self.docs:
            if _matches(d, flt):
                _apply_set(d, update.get("$set", {}))
                n += 1
        return SimpleNamespace(modified_count=n)

    async def create_index(self, keys, **kw):
        self._log.append(("create_index", self.name))
        self.indexes.append((tuple(keys), kw))


def _apply_set(doc, fields):
    for k, v in fields.items():
        parts = k.split(".")
        cur = doc
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v


class FakeDB:
    def __init__(self):
        self._cols = {}
        self.writes = []

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name, self.writes)
        return self._cols[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def mutating_writes(self):
        return [w for w in self.writes if w[0] != "create_index"]
