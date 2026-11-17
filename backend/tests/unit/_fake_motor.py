"""In-memory stand-in for Motor collections, for in-process FastAPI tests.

``patch_motor(testcase)`` patches ``AsyncIOMotorCollection`` *class* methods
(Motor creates a new collection object on every attribute access) so every
collection of ``server.db`` reads and writes one in-memory ``Store``. Supports
the operations the auth flows use: find_one, find (+sort/limit/to_list),
insert_one, update_one, update_many, delete_one, delete_many, count_documents,
find_one_and_update (operator updates and the aggregation-pipeline update used
by ``rate_limit.MongoCounter``), create_index (no-op). Never touches MongoDB.
"""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tests.unit._support import _matches, _apply_set

_MISSING = object()


def _bson_type(v):
    if v is _MISSING:
        return "missing"
    if isinstance(v, datetime):
        return "date"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    return "object"


def _eval(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        return doc.get(expr[1:], _MISSING)
    if isinstance(expr, dict) and len(expr) == 1:
        (op, arg), = expr.items()
        if op == "$cond":
            return _eval(arg[1] if _eval(arg[0], doc) else arg[2], doc)
        if op == "$and":
            return all(_eval(a, doc) for a in arg)
        if op == "$eq":
            return _eval(arg[0], doc) == _eval(arg[1], doc)
        if op == "$type":
            return _bson_type(_eval(arg, doc))
        if op == "$gt":
            a, b = _eval(arg[0], doc), _eval(arg[1], doc)
            if _bson_type(a) != _bson_type(b) or a is _MISSING:
                return False
            return a > b
        if op == "$add":
            return sum(_eval(a, doc) for a in arg)
        if op == "$ifNull":
            v = _eval(arg[0], doc)
            return _eval(arg[1], doc) if v is _MISSING or v is None else v
        raise AssertionError(f"unsupported pipeline operator {op}")
    return expr


def _project(doc, projection):
    if doc is None:
        return None
    doc = copy.deepcopy(doc)
    doc.pop("_mongo_id", None)
    if not projection:
        return doc
    include = {k for k, v in projection.items() if v and k != "_id"}
    if include:
        out = {k: doc[k] for k in include if k in doc}
        if projection.get("_id", 1) and "_id" in doc:
            out["_id"] = doc["_id"]
        return out
    for k, v in projection.items():
        if not v:
            doc.pop(k, None)
    return doc


def _apply_update(doc, update):
    _apply_set(doc, update.get("$set", {}))
    for k in update.get("$unset", {}):
        doc.pop(k, None)
    for k, v in update.get("$inc", {}).items():
        doc[k] = (doc.get(k) or 0) + v
    for k, v in update.get("$addToSet", {}).items():
        lst = doc.setdefault(k, [])
        if v not in lst:
            lst.append(v)
    for k, v in update.get("$push", {}).items():
        lst = doc.setdefault(k, [])
        if isinstance(v, dict) and "$each" in v:
            lst.extend(copy.deepcopy(v["$each"]))
        else:
            lst.append(copy.deepcopy(v))


class _Cursor:
    def __init__(self, docs, projection):
        self._docs, self._projection = docs, projection

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None, length=None):
        n = n if n is not None else length
        return [_project(d, self._projection) for d in self._docs[: n or None]]


class Store:
    def __init__(self):
        self.cols = {}
        self.calls = []

    def col(self, name):
        return self.cols.setdefault(name, [])

    def docs(self, name):
        return self.col(name)


def patch_motor(testcase, store=None):
    from motor.motor_asyncio import AsyncIOMotorCollection
    store = store or Store()

    async def find_one(self, flt=None, projection=None, *a, **k):
        store.calls.append(("find_one", self.name))
        for d in store.col(self.name):
            if _matches(d, flt or {}):
                return _project(d, projection)
        return None

    def find(self, flt=None, projection=None, *a, **k):
        store.calls.append(("find", self.name))
        return _Cursor([d for d in store.col(self.name) if _matches(d, flt or {})], projection)

    async def insert_one(self, doc, *a, **k):
        store.calls.append(("insert_one", self.name))
        store.col(self.name).append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, flt, update, upsert=False, *a, **k):
        store.calls.append(("update_one", self.name))
        for d in store.col(self.name):
            if _matches(d, flt):
                _apply_update(d, update)
                return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)
        if upsert:
            doc = {k2: v for k2, v in flt.items() if not k2.startswith("$") and not isinstance(v, dict)}
            doc.update(copy.deepcopy(update.get("$setOnInsert", {})))
            _apply_update(doc, update)
            store.col(self.name).append(doc)
            return SimpleNamespace(modified_count=0, matched_count=0, upserted_id="fake")
        return SimpleNamespace(modified_count=0, matched_count=0, upserted_id=None)

    async def update_many(self, flt, update, *a, **k):
        n = 0
        for d in store.col(self.name):
            if _matches(d, flt):
                _apply_update(d, update)
                n += 1
        return SimpleNamespace(modified_count=n)

    async def delete_one(self, flt, *a, **k):
        store.calls.append(("delete_one", self.name))
        col = store.col(self.name)
        for i, d in enumerate(col):
            if _matches(d, flt):
                del col[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, flt, *a, **k):
        col = store.col(self.name)
        keep = [d for d in col if not _matches(d, flt)]
        n = len(col) - len(keep)
        col[:] = keep
        return SimpleNamespace(deleted_count=n)

    async def count_documents(self, flt, *a, **k):
        return sum(1 for d in store.col(self.name) if _matches(d, flt))

    async def find_one_and_update(self, flt, update, projection=None, upsert=False, return_document=False, **k):
        store.calls.append(("find_one_and_update", self.name))
        col = store.col(self.name)
        doc = next((d for d in col if _matches(d, flt)), None)
        if doc is None:
            if not upsert:
                return None
            doc = {k2: v for k2, v in flt.items() if not k2.startswith("$")}
            col.append(doc)
        before = copy.deepcopy(doc)
        if isinstance(update, list):
            current = copy.deepcopy(doc)
            for stage in update:
                (op, fields), = stage.items()
                assert op == "$set"
                doc.update({key: _eval(v, current) for key, v in fields.items()})
        else:
            _apply_update(doc, update)
        return _project(doc if return_document else before, projection)

    async def find_one_and_delete(self, flt, projection=None, **k):
        store.calls.append(("find_one_and_delete", self.name))
        col = store.col(self.name)
        for i, d in enumerate(col):
            if _matches(d, flt):
                del col[i]
                return _project(d, projection)
        return None

    async def create_index(self, *a, **k):
        return "noop"

    for name, fn in (("find_one", find_one), ("find", find), ("insert_one", insert_one),
                     ("update_one", update_one), ("update_many", update_many),
                     ("delete_one", delete_one), ("delete_many", delete_many),
                     ("count_documents", count_documents),
                     ("find_one_and_update", find_one_and_update), ("find_one_and_delete", find_one_and_delete),
                     ("create_index", create_index)):
        p = mock.patch.object(AsyncIOMotorCollection, name, fn)
        p.start()
        testcase.addCleanup(p.stop)
    return store


def utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
