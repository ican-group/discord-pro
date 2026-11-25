"""API lookup routing (spec 018): recognise Cardano inputs in a message, choose an imported
read-only endpoint that answers the question, call it through the outbound guard and format the
result for the channel.

Deterministic: no language model chooses the endpoint or the values. The router scores endpoints
from the imported documentation (summary, path, description, parameter descriptions); when two
choices are close, or a required value is missing, it returns one short question instead.

Pure functions plus :func:`call_endpoint` (one guarded GET, via ``api_integrations``).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import api_integrations as apiint

# ═════════════════════════════ Bech32 (BIP-173; no length limit, as CIP-5 allows) ═════════════════════════════

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values: Sequence[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= _GEN[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: Sequence[int], frombits: int, tobits: int, pad: bool) -> Optional[List[int]]:
    acc = bits = 0
    out: List[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return out


def bech32_decode(text: str) -> Tuple[str, bytes]:
    """``(hrp, payload bytes)`` or ValueError with a short reason."""
    if not isinstance(text, str) or len(text) > 1000:
        raise ValueError("not a bech32 string")
    if text.lower() != text and text.upper() != text:
        raise ValueError("mixed upper and lower case")
    s = text.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise ValueError("not a bech32 string")
    hrp, data = s[:pos], s[pos + 1:]
    if any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        raise ValueError("invalid prefix")
    try:
        values = [_CHARSET.index(c) for c in data]
    except ValueError:
        raise ValueError("invalid character")
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("checksum does not match")
    payload = _convertbits(values[:-6], 5, 8, False)
    if payload is None:
        raise ValueError("invalid padding")
    return hrp, bytes(payload)


def bech32_encode(hrp: str, payload: bytes) -> str:
    data = _convertbits(payload, 8, 5, True) or []
    poly = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    return hrp + "1" + "".join(_CHARSET[d] for d in data + [(poly >> 5 * (5 - i)) & 31 for i in range(6)])


# ═════════════════════════════ Cardano addresses (CIP-19) ═════════════════════════════

_PAYMENT_TYPES = {0: "base", 1: "base", 2: "base", 3: "base", 4: "pointer", 5: "pointer", 6: "enterprise", 7: "enterprise"}
_HRP = {"addr": ("payment_address", "mainnet"), "addr_test": ("payment_address", "testnet"),
        "stake": ("stake_address", "mainnet"), "stake_test": ("stake_address", "testnet")}


def parse_cardano_address(text: str) -> Dict[str, Any]:
    """``{kind, network, address_type, value}`` for a Shelley payment (``addr1…``/``addr_test1…``)
    or stake (``stake1…``/``stake_test1…``) address. Checks the bech32 checksum, the header type,
    that the header's network id matches the prefix (1 = mainnet, 0 = testnets) and the payload
    length. Raises ValueError with a reason safe to show."""
    hrp, payload = bech32_decode(text)
    if hrp not in _HRP:
        raise ValueError(f"unknown address prefix '{hrp}'")
    kind, network = _HRP[hrp]
    if not payload:
        raise ValueError("empty address")
    header = payload[0]
    addr_type, net_id = header >> 4, header & 0x0F
    if net_id != (1 if network == "mainnet" else 0):
        raise ValueError(f"the prefix says {network} but the address is for network id {net_id}")
    if kind == "stake_address":
        if addr_type not in (14, 15):
            raise ValueError("not a stake address")
        if len(payload) != 29:
            raise ValueError("wrong length for a stake address")
        sub = "stake key" if addr_type == 14 else "stake script"
    else:
        if addr_type not in _PAYMENT_TYPES:
            raise ValueError("not a payment address")
        sub = _PAYMENT_TYPES[addr_type]
        if (sub == "base" and len(payload) != 57) or (sub == "enterprise" and len(payload) != 29) \
                or (sub == "pointer" and len(payload) < 30):
            raise ValueError(f"wrong length for a {sub} address")
    return {"kind": kind, "network": network, "address_type": sub, "value": text.lower()}


# ═════════════════════════════ governance and pool ids (CIP-129 / CIP-105) ═════════════════════════════

# prefix → (kind, payload lengths, CIP-129 headers allowed for the 29-byte form)
_ID_PREFIXES = {
    "pool": ("pool_id", {28}, set()),
    "drep": ("drep_id", {28, 29}, {0x22, 0x23}),
    "drep_script": ("drep_id", {28}, set()),
    "drep_vkh": ("drep_id", {28}, set()),
    "drep_vk": ("drep_id", {32}, set()),
    "cc_cold": ("committee_credential", {28, 29}, {0x12, 0x13}),
    "cc_cold_script": ("committee_credential", {28}, set()),
    "cc_cold_vkh": ("committee_credential", {28}, set()),
    "cc_cold_vk": ("committee_credential", {32}, set()),
    "cc_hot": ("committee_credential", {28, 29}, {0x02, 0x03}),
    "cc_hot_script": ("committee_credential", {28}, set()),
    "cc_hot_vkh": ("committee_credential", {28}, set()),
    "cc_hot_vk": ("committee_credential", {32}, set()),
    "gov_action": ("gov_action", {33}, set()),
}


def parse_cardano_id(text: str) -> Dict[str, Any]:
    """``{kind, network: None, value}`` for a stake pool, DRep, committee credential or governance
    action id in bech32 form. A governance action also carries ``tx_hash`` and ``index``. Raises
    ValueError with a reason safe to show."""
    hrp, payload = bech32_decode(text)
    if hrp not in _ID_PREFIXES:
        raise ValueError(f"unknown id prefix '{hrp}'")
    kind, lengths, headers = _ID_PREFIXES[hrp]
    if len(payload) not in lengths:
        raise ValueError(f"wrong length for a {KIND_LABELS[kind]}")
    if len(payload) == 29 and payload[0] not in headers:
        raise ValueError(f"the header byte isn't a {KIND_LABELS[kind]}")
    out = {"kind": kind, "network": None, "value": text.lower()}
    if kind == "gov_action":
        out.update(tx_hash=payload[:32].hex(), index=payload[32])
    return out


# ═════════════════════════════ input detection ═════════════════════════════

_ADDR_LIKE = re.compile(r"(?<![0-9A-Za-z_])(?:addr_test|addr|stake_test|stake)1[0-9A-Za-z]{8,}(?![0-9A-Za-z_])", re.I)
_ID_LIKE = re.compile(r"(?<![0-9A-Za-z_])(?:pool|drep_script|drep_vkh|drep_vk|drep|cc_cold_script|cc_cold_vkh|cc_cold_vk|cc_cold"
                      r"|cc_hot_script|cc_hot_vkh|cc_hot_vk|cc_hot|gov_action)1[0-9A-Za-z]{8,}(?![0-9A-Za-z_])", re.I)
_HEX64 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
# "Paddr1…": an address with stray characters stuck to the front.
_ADDR_GLUED = re.compile(r"(?<![0-9A-Za-z_])([0-9A-Za-z]{1,8})((?:addr_test|addr|stake_test|stake)1[0-9A-Za-z]{20,})(?![0-9A-Za-z_])")
_TX_INDEX = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{64})#(\d{1,3})(?!\d)")
# A number only counts with its cue word right before it ("epoch 512", "slot #1234", "label 674").
_NUMBER = re.compile(r"\b(epochs?|slot|block|label|index)\s*(?:no\.?|num(?:ber)?\.?|#)?\s*#?(\d{1,12})(?![\w.])", re.I)
_NUMBER_KINDS = {"epoch": "epoch_number", "epochs": "epoch_number", "slot": "slot_number", "block": "block_number",
                 "label": "metadata_label", "index": "proposal_index"}
NUMBER_CUES = {"epoch_number": "epoch", "slot_number": "slot", "block_number": "block",
               "metadata_label": "label", "proposal_index": "index"}
_WORD = re.compile(r"[a-z][a-z0-9]*")
_TX_CUES = {"transaction", "transactions", "tx", "txs", "txn", "txid", "utxo", "utxos", "metadata"}
_BLOCK_CUES = {"block", "blocks"}
_GOV_CUES = {"proposal", "proposals", "action"}
_TX_SOFT_CUES = {"fee", "fees", "sent", "send", "payment", "paid", "transfer", "confirmed", "confirmation", "confirmations"}
KIND_LABELS = {"payment_address": "payment address", "stake_address": "stake address",
               "tx_hash": "transaction hash", "block_hash": "block hash", "hash": "64-character hash",
               "pool_id": "stake pool id", "drep_id": "DRep id", "committee_credential": "committee credential",
               "gov_action": "governance action id", "epoch_number": "epoch number", "slot_number": "slot number",
               "block_number": "block number", "metadata_label": "metadata label", "proposal_index": "proposal index"}


# Sentences that only say how to format the reply ("Reply with one JSON code block containing
# txHash…", "Use null for…", "If no fresh lookup runs, say so.") aren't part of what to look up.
_FORMAT_SENTENCE = re.compile(
    r"^\W*(please\s+)?(reply|respond|answer|format|return|output|use|include|list each|for each|make sure|do not|don't"
    r"|if\s+(no|you|the|it|nothing|a|any)|only|keep|add|put|wrap)\b"
    r"|\b(json|code ?block|markdown|yaml|csv|plain text|bullet points?)\b", re.I)


def intent_text(text: Any) -> str:
    """The part of a message that says what to look up: sentences that only give formatting
    instructions are left out, unless they carry the address, hash or id (then they stay)."""
    text = text if isinstance(text, str) else ""
    parts = [p for p in re.split(r"(?<=[.!?])\s+|\n+|\s{2,}|\s+(?=(?:please\s+)?(?:reply|respond|answer|format|return)\s+(?:with|in|as|using)\b)",
                                 text, flags=re.I) if p and p.strip()]
    kept = [p for p in parts if not _FORMAT_SENTENCE.search(p) or _ADDR_LIKE.search(p) or _ID_LIKE.search(p)
            or _HEX64.search(p) or _ADDR_GLUED.search(p)]
    return " ".join(kept) if kept else text


def short(value: str) -> str:
    return value if len(value) <= 24 else f"{value[:12]}…{value[-8:]}"


def detect_inputs(text: Any) -> Dict[str, Any]:
    """Cardano inputs in a message: ``{inputs: [...], invalid: [...], words: set}``.

    - Addresses (CIP-19) and pool / DRep / committee / governance-action ids (bech32).
    - A 64-hex value is a ``tx_hash`` or ``block_hash`` from the wording, else an ambiguous
      ``hash``. A hash a proposal refers to (``gov_action1…``, ``<hash>#<n>``, or "proposal"
      right before it) is a ``tx_hash`` with ``role: proposal``.
    - Numbers only with their cue word: ``epoch 512`` → ``epoch_number``…"""
    text = text[:20000] if isinstance(text, str) else ""
    words = set(_WORD.findall(intent_text(text).lower()))
    inputs: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    seen = set()

    def add(item: Dict[str, Any]) -> None:
        key = (item["kind"], item["value"])
        if key not in seen:
            seen.add(key)
            inputs.append(item)

    for m in _ADDR_LIKE.finditer(text):
        raw = m.group(0)
        try:
            add(parse_cardano_address(raw))
        except ValueError as exc:
            invalid.append({"value": raw, "reason": str(exc),
                            "kind": "stake_address" if raw.lower().startswith("stake") else "payment_address"})
    for m in _ADDR_GLUED.finditer(text):
        extra, rest = m.group(1), m.group(2)
        invalid.append({"value": m.group(0), "kind": "stake_address" if rest.lower().startswith("stake") else "payment_address",
                        "reason": f"it has extra characters '{extra}' before '{rest[:5]}…'"})
    for m in _ID_LIKE.finditer(text):
        raw = m.group(0)
        try:
            found = parse_cardano_id(raw)
        except ValueError as exc:
            prefix = raw.lower().rsplit("1", 1)[0]
            prefix = next((p for p in sorted(_ID_PREFIXES, key=len, reverse=True) if raw.lower().startswith(p + "1")), prefix)
            invalid.append({"value": raw, "reason": str(exc), "kind": _ID_PREFIXES.get(prefix, ("pool_id",))[0]})
            continue
        if found["kind"] == "gov_action":
            add({"kind": "tx_hash", "network": None, "value": found["tx_hash"], "role": "proposal"})
            add({"kind": "proposal_index", "network": None, "value": str(found["index"])})
        else:
            add(found)
    stripped = _ID_LIKE.sub(" ", _ADDR_LIKE.sub(" ", text))
    for m in _TX_INDEX.finditer(stripped):
        add({"kind": "tx_hash", "network": None, "value": m.group(1).lower(), "role": "proposal"})
        add({"kind": "proposal_index", "network": None, "value": str(int(m.group(2)))})
    hashed = {i["value"] for i in inputs if i["kind"] == "tx_hash"}
    for m in _HEX64.finditer(stripped):
        h = m.group(0).lower()
        if h in hashed:
            continue
        hashed.add(h)
        # The cue word right before the hash wins ("transactions in block <hash>", "block hash
        # <hash>", "which block holds tx <hash>", "votes on proposal <hash>"); otherwise any cue in
        # the message decides.
        near = next((w for w in reversed(_WORD.findall(stripped[:m.start()].lower())[-3:])
                     if w in _TX_CUES or w in _BLOCK_CUES or w in _GOV_CUES), None)
        role = None
        if near in _GOV_CUES:
            kind, role = "tx_hash", "proposal"
        elif near:
            kind = "block_hash" if near in _BLOCK_CUES else "tx_hash"
        elif words & _TX_CUES:
            kind = "tx_hash"
        elif words & _BLOCK_CUES:
            kind = "block_hash"
        elif words & _TX_SOFT_CUES:
            kind = "tx_hash"
        else:
            kind = "hash"
        add({"kind": kind, "network": None, "value": h, **({"role": role} if role else {})})
    for m in _NUMBER.finditer(_HEX64.sub(" ", stripped)):
        add({"kind": _NUMBER_KINDS[m.group(1).lower()], "network": None, "value": str(int(m.group(2)))})
    return {"inputs": inputs, "invalid": invalid, "words": words}


def saved_lookup_hash(found: Mapping[str, Any]) -> Optional[str]:
    """The hash a saved transaction lookup (spec 015) answers first: a transaction hash, or a bare
    hash with no wording (the existing default). An explicit block hash, or a hash a proposal refers
    to, goes to the router."""
    return next((i["value"] for i in found.get("inputs") or []
                 if i["kind"] in ("tx_hash", "hash") and i.get("role") != "proposal"), None)


# ═════════════════════════════ what an endpoint needs ═════════════════════════════

def param_kinds(param: Mapping[str, Any], path: str) -> List[str]:
    """Input kinds a documented parameter accepts, from its name and description."""
    name = str(param.get("name") or "").lower()
    desc = str(param.get("description") or "").lower()
    text = f"{name} {desc}"
    p = path.lower()
    kinds: List[str] = []
    if "pool" in name or "pool1" in desc:
        return ["pool_id"]
    if "drep" in name or "drep1" in desc:
        return ["drep_id"]
    if "cc_cold" in desc or "cc_hot" in desc or ("credential" in name and "committee" in text):
        return ["committee_credential"]
    if name == "epoch_number" or (name in ("number", "epoch") and "/epochs/" in p):
        return ["epoch_number"]
    if name in ("slot_number", "slot"):
        return ["slot_number"]
    if name == "label" and "/metadata/" in p:
        return ["metadata_label"]
    if name in ("cert_index", "index", "proposal_index") and "/proposals/" in p:
        return ["proposal_index"]
    if "stake" in name or re.search(r"stake address|reward address|stake1|stake_test1", text):
        kinds.append("stake_address")
    elif name in ("address", "addr", "payment_address", "addresses") or \
            re.search(r"payment address|addr1|addr_test1|bech32 address", text):
        kinds.append("payment_address")
    if "hash" in text or "hash" in name:
        if re.search(r"transaction|\btx", text) or re.search(r"/txs?/|/transactions?/", p):
            kinds.append("tx_hash")
        if "block" in text or re.search(r"/blocks?/", p):
            kinds.append("block_hash")
            if "block number" in desc or "number" in name:
                kinds.append("block_number")
    return kinds


MAX_REQUIRED = 3


def endpoint_requirements(ep: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Required parameters with the kinds they accept, or None when one can't be filled from a
    recognised input (asset units, policy ids…)."""
    reqs = []
    for p in ep.get("parameters") or []:
        if p.get("in") == "path" or p.get("required"):
            kinds = param_kinds(p, ep.get("path") or "")
            if not kinds:
                return None
            reqs.append({"name": p["name"], "in": p["in"], "kinds": kinds, "param": p})
    return reqs


# What a value the router can't read from a message is, from the parameter's name and description.
_UNREADABLE = [
    ("asset", r"\bpolicy\b|policy_id|\basset\b|native-asset", "native asset or policy id"),
]


def _unreadable_value(p: Mapping[str, Any]) -> Dict[str, str]:
    name = str(p.get("name") or "")
    text = f"{name} {str(p.get('description') or '')}".lower().replace("_", " ")
    for category, pattern, label in _UNREADABLE:
        if re.search(pattern, f"{name.lower()} {text}"):
            if category == "asset":
                label = "minting policy id" if "policy" in name.lower() else "native asset unit (policy id + asset name)"
            return {"category": category, "label": label, "param": name}
    if p.get("type") in ("integer", "number") or re.search(r"\binteger\b|\bnumber\b", text):
        nice = name.replace("_", " ")
        if nice == "number":   # a bare name: take it from the description ("Epoch number (0 is …)")
            m = re.match(r"\s*([a-z-]+ number)\b", str(p.get("description") or "").lower())
            nice = m.group(1) if m else nice
        return {"category": "number", "label": f"a number ({nice})", "param": name}
    return {"category": "other", "label": f"'{name}'", "param": name}


def bot_usability(ep: Mapping[str, Any]) -> Dict[str, Any]:
    """Whether the router can use an imported endpoint, the inputs it accepts and, when it can't,
    why: ``{usable, accepts, needs (kinds per required value), reason: {category, label, param} | None}``. Same rule as ``route``:
    GET, and every required value (at most ``MAX_REQUIRED``) comes from a recognised input."""
    if str(ep.get("method") or "").upper() != "GET":
        return {"usable": False, "accepts": [], "reason": {"category": "method", "label": "only GET (read-only) calls", "param": ""}}
    reqs = endpoint_requirements(ep)
    if reqs is None:
        blocker = next(p for p in ep.get("parameters") or []
                       if (p.get("in") == "path" or p.get("required")) and not param_kinds(p, ep.get("path") or ""))
        return {"usable": False, "accepts": [], "reason": _unreadable_value(blocker)}
    if len(reqs) > MAX_REQUIRED:
        return {"usable": False, "accepts": [], "reason": {"category": "multiple", "label": f"more than {MAX_REQUIRED} values from the message",
                                                            "param": ", ".join(r["name"] for r in reqs)}}
    accepts: List[str] = []
    for r in reqs:
        accepts += [k for k in r["kinds"] if k not in accepts]
    return {"usable": True, "accepts": accepts, "needs": [list(r["kinds"]) for r in reqs], "reason": None}


def _check_schema(value: str, param: Mapping[str, Any]) -> Optional[str]:
    """A reason when the value breaks the documented constraints of the parameter."""
    if param.get("pattern"):
        try:
            if not re.search(param["pattern"], value):
                return f"doesn't match the documented format for '{param['name']}'"
        except re.error:
            pass
    if param.get("min_length") and len(value) < int(param["min_length"]):
        return f"is shorter than '{param['name']}' allows"
    if param.get("max_length") and len(value) > int(param["max_length"]):
        return f"is longer than '{param['name']}' allows"
    if param.get("enum") and value not in [str(x) for x in param["enum"]]:
        return f"isn't one of the documented values for '{param['name']}'"
    return None


# ═════════════════════════════ intent scoring ═════════════════════════════

_CONCEPTS = {
    "balance": "balance balances amount controlled held holding holdings funds worth",
    "reward": "reward rewards earned earning earnings",
    "fee": "fee fees cost",
    "status": "status info information tip health healthy state sync synced",
    "network": "network chain blockchain mainnet",
    "latest": "latest current newest last tip",
    "recent": "recent",
    "delegation": "delegation delegations delegated delegate delegating pool pools staked",
    "history": "history historical",
    "asset": "asset assets token tokens nft nfts native",
    "utxo": "utxo utxos unspent input inputs output outputs",
    "transaction": "transaction transactions tx txs txn txns",
    "metadata": "metadata",
    "epoch": "epoch epochs",
    "block": "block blocks",
    "address": "address addresses",
    "withdrawal": "withdrawal withdrawals withdrawn withdraw",
    "registration": "registration registrations registered",
    "price": "price prices usd",
    "stats": "stats statistics",
    "total": "total totals sum received sent",
    "count": "count many number",
    "parameters": "parameters params parameter",
    "governance": "governance constitution treasury",
    "proposal": "proposal proposals",
    "vote": "vote votes voting voted",
    "drep": "drep dreps",
    "committee": "committee",
    "supply": "supply circulating",
}
_WORD_CONCEPT = {w: c for c, ws in _CONCEPTS.items() for w in ws.split()}
_STOP = set("a an the of for to in on at by is are was were be been do does did what whats which who how much "
            "my me i you your our this that these those it its and or with from please can could would show tell "
            "check look up lookup find give get about there here as has have had any some all api v0 v1 v2 id".split())
_PERSONAL = re.compile(r"\b(my|mine|our|ours)\b", re.I)
_HOW_TO = re.compile(r"\b(how (do|does|can|to|should|would)|why|explain|what (is|are) (a|an)\b|meaning of|difference between)", re.I)
# A question without an address or hash only triggers a call when it asks for live data.
LIVE_CONCEPTS = {"latest", "recent", "status", "price", "stats", "supply"}


def _known(concepts: set) -> set:
    return {c for c in concepts if c in _CONCEPTS}


def _concepts(text: str) -> set:
    out = set()
    for w in _WORD.findall(text.lower().replace("_", " ").replace("-", " ")):
        if w in _STOP or len(w) < 2:
            continue
        out.add(_WORD_CONCEPT.get(w) or _WORD_CONCEPT.get(w.rstrip("s")) or w.rstrip("s"))
    return out


def _literals(path: str) -> List[str]:
    segs = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    return [s for s in segs if not re.fullmatch(r"(api|v\d+(\.\d+)?)", s.lower())]


def score_endpoint(question: set, ep: Mapping[str, Any]) -> float:
    """How well an endpoint's documentation matches the question's concepts. Each known concept
    scores by where it appears: 4 in the path, 3 in the summary, 2 in the description's first
    sentence, 1 elsewhere in the description or tags; other words score 2 when they appear in the
    path or summary. −1.2 for each concept in a path segment after the first that the question
    doesn't mention (prefers the plain resource); +0.5 for a path that ends with a parameter."""
    path = ep.get("path") or ""
    desc = str(ep.get("description") or "")
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0][:200]
    in_path = _concepts(" ".join(_literals(path)))
    in_summary = _concepts(str(ep.get("summary") or ""))
    in_first = _concepts(first)
    weak = _concepts(f"{desc} {' '.join(ep.get('tags') or [])}")
    known = _known(question)
    score = sum(4.0 if c in in_path else 3.0 if c in in_summary else 2.0 if c in in_first else 1.0 if c in weak else 0.0
                for c in known)
    score += sum(2.0 for c in question - known if (c in in_path or c in in_summary) and len(c) > 3)
    penalised = set()
    for seg in _literals(path)[1:]:
        for c in _concepts(seg) - question - penalised:
            penalised.add(c)
            score -= 1.2
    if path.rstrip("/").endswith("}"):
        score += 0.5
    return score


AMBIGUITY_MARGIN = 1.0
NO_INPUT_MIN = 3.0
MISSING_VALUE_MIN = 2.0


def _phrase(ep: Mapping[str, Any]) -> str:
    return (ep.get("summary") or ep.get("path") or "").strip().rstrip(".").lower()


def route(question: str, providers: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Decide what to do with a message.

    ``providers``: ``[{integration, endpoints}]`` — bot-enabled integrations with their usable GET
    endpoints (disabled ones already removed); ``integration['network']`` is ``mainnet``, ``testnet``
    or ``any``.

    Returns ``{action, ...}``:
    - ``none``: nothing to look up (no input and no clear no-input intent);
    - ``ask``: ``question`` — one short question (ambiguous hash or intent, missing value);
    - ``reject``: ``message`` — invalid input or a network the providers don't serve;
    - ``call``: ``integration``, ``endpoint``, ``values``, ``input``, ``score``, ``alternatives``.
    """
    found = detect_inputs(question)
    inputs, invalid = found["inputs"], found["invalid"]
    intent = intent_text(question)
    q = _concepts(intent)
    base = {"inputs": inputs, "invalid": invalid}
    if invalid and not inputs:
        bad = invalid[0]
        return {**base, "action": "reject", "reason": "invalid_input",
                "message": f"That {KIND_LABELS[bad['kind']]} doesn't look valid ({bad['reason']}). "
                           "Please check it and send it again."}
    hashes = [i for i in inputs if i["kind"] == "hash"]
    if hashes and len(inputs) == 1:
        # A bare hash: most are transactions. Try it as one, and as a block if the provider has no
        # such transaction (the server calls ``fallback`` on "not found"). Ask only when neither works.
        as_tx = route(f"{question} transaction", providers)
        as_block = route(f"{question} block", providers)
        if as_tx["action"] == "call":
            return {**as_tx, "fallback": as_block if as_block["action"] == "call" else None}
        if as_block["action"] == "call":
            return as_block
        return {**base, "action": "ask", "reason": "hash_kind",
                "question": f"Is {short(hashes[0]['value'])} a transaction hash or a block hash?"}

    candidates: List[Dict[str, Any]] = []
    partial: List[Dict[str, Any]] = []      # one required value missing, at least one given
    mismatched: List[Tuple[Dict[str, Any], Mapping[str, Any]]] = []
    for prov in providers:
        integ = prov["integration"]
        net = integ.get("network") or "any"
        usable = []
        for inp in inputs:
            if inp.get("network") and net not in ("any", inp["network"]):
                mismatched.append((inp, integ))
            else:
                usable.append(inp)
        for ep in prov["endpoints"]:
            reqs = endpoint_requirements(ep)
            if reqs is None or len(reqs) > MAX_REQUIRED:
                continue
            if not reqs:
                if not inputs:
                    candidates.append({"integration": integ, "endpoint": ep, "values": {}, "input": None,
                                       "score": score_endpoint(q, ep)})
                continue
            score = None
            for values, used, missing in _assignments(reqs, usable):
                if len(missing) > 1 or (missing and not used):
                    continue
                if score is None:
                    score = score_endpoint(q, ep)
                # Prefer the endpoint that uses more of what was given; a hash a proposal refers to
                # belongs to the proposal endpoints.
                sc = score + 1.5 * (len(used) - 1) - sum(2.0 for i in used if i.get("role") == "proposal"
                                                         and "/proposals/" not in (ep.get("path") or "").lower())
                entry = {"integration": integ, "endpoint": ep, "values": values, "input": used[0], "score": sc}
                if missing:
                    partial.append({**entry, "missing": missing[0]})
                else:
                    candidates.append(entry)

    partial.sort(key=lambda c: -c["score"])
    if partial and not _HOW_TO.search(intent) and partial[0]["score"] >= MISSING_VALUE_MIN \
            and (not candidates or partial[0]["score"] > max(c["score"] for c in candidates) + AMBIGUITY_MARGIN):
        need = partial[0]
        kind = need["missing"]["kinds"][0]
        target = f" for {short(need['input']['value'])}" if need["input"]["kind"] not in ("proposal_index",) else ""
        return {**base, "action": "ask", "reason": "missing_value", "expect": kind,
                "question": ASK_FOR.get(kind, f"Which {KIND_LABELS.get(kind, kind)} should I look up?").replace("{target}", target),
                "endpoint": need["endpoint"], "integration": need["integration"]}

    if not candidates:
        if mismatched:
            inp, integ = mismatched[0]
            other = "mainnet" if inp["network"] == "testnet" else "a testnet"
            prefix = {"payment_address": "addr1…", "stake_address": "stake1…"}[inp["kind"]] if other == "mainnet" \
                else {"payment_address": "addr_test1…", "stake_address": "stake_test1…"}[inp["kind"]]
            return {**base, "action": "reject", "reason": "network",
                    "message": f"That's a {inp['network']} {KIND_LABELS[inp['kind']]}, but {integ['name']} serves "
                               f"Cardano {integ.get('network')} data. Please send a {other} address ({prefix})."}
        if not inputs and _PERSONAL.search(intent) and not _HOW_TO.search(intent):
            need = _missing_value(q, providers)
            if need:
                return {**base, "action": "ask", "reason": "missing_value", **need}
        return {**base, "action": "none"}

    candidates.sort(key=lambda c: -c["score"])
    # A question that names a network only uses providers for that network.
    asked_net = "testnet" if re.search(r"\b(testnet|preprod|preview)\b", intent, re.I) else \
        "mainnet" if re.search(r"\bmainnet\b", intent, re.I) else None
    if asked_net:
        on_net = [c for c in candidates if (c["integration"].get("network") or "any") in ("any", asked_net)]
        candidates = on_net or candidates
    best = candidates[0]
    if not inputs and (best["score"] < NO_INPUT_MIN or not (q & LIVE_CONCEPTS) or _HOW_TO.search(intent)):
        if _PERSONAL.search(intent) and not _HOW_TO.search(intent):
            need = _missing_value(q, providers)
            if need:
                return {**base, "action": "ask", "reason": "missing_value", **need}
        return {**base, "action": "none"}
    close = [c for c in candidates[1:] if best["score"] - c["score"] < AMBIGUITY_MARGIN]
    # The same question answered by providers for different networks: ask which network, unless the
    # input itself says (addresses carry their network; hashes don't).
    net = lambda c: c["integration"].get("network") or "any"
    other_net = [c for c in close if c["integration"] is not best["integration"] and net(c) != net(best)
                 and "any" not in (net(c), net(best)) and not (best["input"] or {}).get("network")]
    if other_net and (inputs or best["score"] >= NO_INPUT_MIN):
        a, b = best, other_net[0]
        return {**base, "action": "ask", "reason": "network",
                "question": f"Do you mean Cardano {net(a)} ({a['integration']['name']}) or {net(b)} ({b['integration']['name']})?",
                "options": [a, b]}
    # Close alternatives on a different endpoint of the same provider: the intent is unclear.
    rivals = [c for c in close if c["integration"] is best["integration"] and c["score"] > 0.5
              and _phrase(c["endpoint"]) != _phrase(best["endpoint"])]
    if rivals and best["score"] > 0.5:
        if not inputs:          # a vague question with no address or hash: leave it to the knowledge base
            return {**base, "action": "none"}
        target = f" for {short(best['input']['value'])}" if best["input"] else ""
        return {**base, "action": "ask", "reason": "ambiguous_intent",
                "question": f"Do you want the {_phrase(best['endpoint'])} or the {_phrase(rivals[0]['endpoint'])}{target}?",
                "options": [best, rivals[0]]}
    return {**base, "action": "call", **best,
            "alternatives": [{"endpoint": c["endpoint"]["id"], "provider": c["integration"]["name"], "score": round(c["score"], 2)}
                             for c in candidates[1:4]]}


def _assignments(reqs: Sequence[Mapping[str, Any]], inputs: Sequence[Mapping[str, Any]]):
    """Ways to fill the required values from distinct inputs: ``(values, used inputs, missing reqs)``.
    Values that break the documented constraints don't fill a requirement."""
    out: List[Tuple[Dict[str, str], List[Mapping[str, Any]], List[Mapping[str, Any]]]] = []

    def walk(i: int, values: Dict[str, str], used: List[Mapping[str, Any]], missing: List[Mapping[str, Any]]) -> None:
        if len(out) >= 50 or len(missing) > 1:
            return
        if i == len(reqs):
            out.append((dict(values), list(used), list(missing)))
            return
        req = reqs[i]
        options = [inp for inp in inputs if inp["kind"] in req["kinds"] and inp not in used
                   and not _check_schema(inp["value"], req["param"])]
        for inp in options:
            values[req["name"]] = inp["value"]
            walk(i + 1, values, used + [inp], missing)
            del values[req["name"]]
        if not options:
            walk(i + 1, values, used, missing + [req])

    walk(0, {}, [], [])
    return out


ASK_FOR = {
    "stake_address": "Which stake address (stake1…) should I look up?",
    "payment_address": "Which address (addr1…) should I look up?",
    "tx_hash": "Which transaction hash should I look up?",
    "block_hash": "Which block hash should I look up?",
    "pool_id": "Which stake pool (pool1…) should I look up?",
    "drep_id": "Which DRep (drep1…) should I look up?",
    "committee_credential": "Which committee member (cc_cold1… or cc_hot1…) should I look up?",
    "epoch_number": "Which epoch number{target}?",
    "slot_number": "Which slot number{target}?",
    "metadata_label": "Which metadata label (for example 674)?",
    "proposal_index": "Which proposal index in that transaction? It's usually 0.",
}


# "my …" questions ask for something the user has: their address, stake address or transaction.
_OWN_KINDS = {"stake_address", "payment_address", "tx_hash"}


def _missing_value(q: set, providers: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """When the question clearly asks for something that needs a value that isn't there:
    ``{question, expect}``."""
    known = _known(q)
    if not known:
        return None
    scored: List[Tuple[float, str]] = []
    for prov in providers:
        for ep in prov["endpoints"]:
            reqs = endpoint_requirements(ep)
            if not reqs or len(reqs) != 1 or reqs[0]["kinds"][0] not in _OWN_KINDS:
                continue
            scored.append((score_endpoint(known, ep), reqs[0]["kinds"][0]))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < MISSING_VALUE_MIN:
        return None
    kinds = []
    for sc, kind in scored:
        if scored[0][0] - sc < AMBIGUITY_MARGIN and kind not in kinds:
            kinds.append(kind)
    labels = {"stake_address": "stake address (stake1…)", "payment_address": "address (addr1…)",
              "tx_hash": "transaction hash", "block_hash": "block hash", "pool_id": "stake pool (pool1…)",
              "drep_id": "DRep (drep1…)", "committee_credential": "committee member (cc_cold1… or cc_hot1…)",
              "epoch_number": "epoch number", "slot_number": "slot number", "metadata_label": "metadata label",
              "proposal_index": "proposal index", "block_number": "block number"}
    if len(kinds) == 1 and kinds[0] in ASK_FOR:
        return {"question": ASK_FOR[kinds[0]].replace("{target}", ""), "expect": kinds[0]}
    return {"question": f"Which {' or '.join(labels.get(k, k) for k in kinds[:2])} should I look up?",
            "expect": kinds[0] if len(kinds) == 1 else None}


# ═════════════════════════════ follow-up answers (spec 019) ═════════════════════════════

PENDING_SECONDS = 600
_FOLLOW_UP_WORDS = {"mainnet", "testnet", "preprod", "preview"} | _TX_CUES | _BLOCK_CUES | _GOV_CUES


def follow_up_text(original: str, reply: Any, expect: Optional[str]) -> Optional[str]:
    """The remembered question plus a short reply that looks like its answer, or None. A bare number
    gets the cue word of the kind asked for ("512" → "epoch 512")."""
    reply = reply.strip() if isinstance(reply, str) else ""
    if not reply or len(reply) > 200 or len(reply.split()) > 12:
        return None
    number = re.fullmatch(r"#?\s*(\d{1,12})\.?", reply)
    if number:
        if expect not in NUMBER_CUES:
            return None
        reply = f"{NUMBER_CUES[expect]} {number.group(1)}"
    found = detect_inputs(reply)
    if not (found["inputs"] or found["invalid"] or _known(_concepts(reply)) or found["words"] & _FOLLOW_UP_WORDS):
        return None          # "thanks", "ok"…: not an answer
    return f"{original} {reply}"


def answers_expected(reply: Any, expect: Optional[str]) -> bool:
    """The reply supplies exactly the kind of value the bot asked for."""
    if not expect or not isinstance(reply, str):
        return False
    if expect in NUMBER_CUES and re.fullmatch(r"#?\s*\d{1,12}\.?", reply.strip()):
        return True
    return any(i["kind"] == expect for i in detect_inputs(reply)["inputs"])


# ═════════════════════════════ what the bot can look up (for its prompt) ═════════════════════════════

_INPUT_PHRASES = {"payment_address": "a Cardano address (addr1…)", "stake_address": "a stake address (stake1…)",
                  "tx_hash": "a transaction hash", "block_hash": "a block hash", "pool_id": "a stake pool id (pool1…)",
                  "drep_id": "a DRep id (drep1…)", "epoch_number": "an epoch number (\"epoch 512\")"}
_EXAMPLES = {"stake_address": "What are the staking rewards for stake1…?", "payment_address": "What's the balance of addr1…?",
             "tx_hash": "What was the fee for transaction <hash>?"}


def capability_note(providers: Sequence[Mapping[str, Any]], saved_tx_lookup: bool = False) -> str:
    """What the bot can look up live, for its prompt when no lookup ran (or alongside one), so it
    never tells users it can't query the API. Empty when nothing is available."""
    kinds: List[str] = ["tx_hash"] if saved_tx_lookup else []
    live = False
    names: List[str] = []
    for prov in providers:
        integ = prov["integration"]
        net = integ.get("network") or "any"
        names.append(f"{integ['name']}" + (f" (Cardano {net})" if net != "any" else ""))
        for ep in prov["endpoints"]:
            u = bot_usability(ep)
            if not u["usable"]:
                continue
            for k in u["accepts"]:
                if k in _INPUT_PHRASES and k not in kinds:
                    kinds.append(k)
            if not u["accepts"] and _concepts(f"{ep.get('summary') or ''} {' '.join(_literals(ep.get('path') or ''))}") & LIVE_CONCEPTS:
                live = True
    if not kinds and not live:
        return ""
    order = ["stake_address", "payment_address", "tx_hash", "block_hash", "pool_id", "drep_id", "epoch_number"]
    kinds.sort(key=order.index)
    wants = [_INPUT_PHRASES[k] for k in kinds]
    triggers = []
    if wants:
        triggers.append("the message includes " + (", ".join(wants[:-1]) + " or " + wants[-1] if len(wants) > 1 else wants[0]))
    if live:
        triggers.append("it asks for current network data, such as the network status, the latest block or the current epoch")
    examples = [_EXAMPLES[k] for k in kinds if k in _EXAMPLES][:2] + (["What's the network status?"] if live else [])
    source = ", ".join(names) if names else "this workspace's saved lookup"
    return (f"You can look up live data from {source}. BridgeBot runs the lookup automatically when "
            f"{', or when '.join(triggers)}. Example questions: " + "; ".join(f'"{e}"' for e in examples) + ".")


# What was last looked up in a conversation, so "show me the inputs for this transaction" works.
SUBJECT_SECONDS = 1800
_BACK_REF = re.compile(r"\b(this|that|it|its|same|those|these|them|the (transaction|tx|block|address|pool|account|proposal))\b"
                       r"|^\s*(and|also|what about|how about)\b", re.I)


_QUESTION_START = re.compile(r"\s*(what|which|when|where|who|how|is|was|were|did|does|do|has|had|can|could|show|list|give|tell)\b", re.I)


def subject_text(found_inputs: Sequence[Mapping[str, Any]], values: Mapping[str, str]) -> str:
    """The inputs a call used, written so the router recognises them again ("transaction <hash>")."""
    used = set(values.values())
    parts = []
    for i in found_inputs:
        if i["value"] not in used:
            continue
        if i["kind"] == "tx_hash":
            parts.append(f"{'proposal' if i.get('role') == 'proposal' else 'transaction'} {i['value']}")
        elif i["kind"] == "block_hash":
            parts.append(f"block {i['value']}")
        elif i["kind"] in NUMBER_CUES:
            parts.append(f"{NUMBER_CUES[i['kind']]} {i['value']}")
        else:
            parts.append(i["value"])
    return " ".join(parts)


def about_subject(text: Any) -> bool:
    """A message with no input of its own that asks about the last thing looked up."""
    if not isinstance(text, str):
        return False
    text = intent_text(text)
    if len(text) > 300 or _HOW_TO.search(text):
        return False
    found = detect_inputs(text)
    asks = "?" in text or bool(_known(_concepts(text))) or bool(_QUESTION_START.match(text)) \
        or bool({"show", "list", "detail", "details", "more"} & found["words"])
    return not found["inputs"] and not found["invalid"] and bool(_BACK_REF.search(text)) and asks


def general_question(question: str) -> bool:
    """A question about a thing as a whole ("tell me about this transaction", a bare hash), not
    about one detail of it."""
    return not (_known(_concepts(question)) - {"transaction", "block", "address", "status"})


def related_endpoint(ep: Mapping[str, Any], endpoints: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """For a record endpoint, the provider's endpoint for its inputs and outputs (GridScan's
    ``/txs/{hash}/utxos``), so a general answer can say where the funds came from and went."""
    reqs = endpoint_requirements(ep) or []
    if [r["kinds"] for r in reqs] != [["tx_hash"]]:
        return None                       # only transactions have inputs and outputs worth fetching
    base = (ep.get("path") or "").rstrip("/")
    for other in endpoints:
        path = (other.get("path") or "").rstrip("/")
        if path.startswith(base + "/") and path.count("/") == base.count("/") + 1 \
                and "utxo" in _concepts(path[len(base):]) and endpoint_requirements(other) == endpoint_requirements(ep):
            return other
    return None


# ═════════════════════════════ the call ═════════════════════════════

def _optional_query(ep: Mapping[str, Any]) -> Dict[str, str]:
    """Newest first and a short page when the endpoint documents ``order``/``count``-style options.
    A UTxO set is current state, not history: it keeps the API's own page size and order."""
    out = {}
    literals = _literals(ep.get("path") or "")
    if literals and "utxo" in _concepts(literals[-1]):
        return out
    for p in ep.get("parameters") or []:
        if p.get("in") != "query" or p.get("required"):
            continue
        name, desc = p["name"].lower(), str(p.get("description") or "").lower()
        if name == "order" and ("desc" in desc or "desc" in [str(x).lower() for x in p.get("enum") or []]):
            out[p["name"]] = "desc"
        elif name in ("count", "limit", "page_size", "per_page") and p.get("type") in ("integer", "number", "string"):
            out[p["name"]] = "10"
    return out


async def call_endpoint(integration: Mapping[str, Any], ep: Mapping[str, Any], values: Mapping[str, str],
                        secret: Optional[str]) -> Dict[str, Any]:
    """One guarded GET. ``status``: ``ok`` (with ``data``), ``not_found`` (HTTP 404 where the
    documentation says 404 means an unknown id, or an empty object/null), ``unavailable`` (anything
    else, with ``reason``). Same outbound rules and error wording as lookups."""
    result: Dict[str, Any] = {"status": "unavailable", "reason": "", "message": "", "http_status": None,
                              "duration_ms": 0, "request_url": None}
    try:
        req = apiint.build_endpoint_request(integration, ep, {**_optional_query(ep), **values}, secret)
    except (apiint.ConfigError, apiint.OutboundBlocked) as exc:
        result.update(reason="configuration", message=str(exc))
        return result
    result["request_url"] = req["display_url"]
    started = time.monotonic()
    try:
        resp = await apiint.guarded_get(req["origin"], req["host"], req["port"], req["target"], req["headers"])
    except apiint.OutboundBlocked as exc:
        result.update(reason="blocked", message=str(exc)); return result
    except apiint.OutboundError as exc:
        result.update(reason="network", message=str(exc)); return result
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
    st = result["http_status"] = resp["status"]
    msgs = {401: ("auth", "The API refused the request (HTTP {}). Check the API key."),
            403: ("auth", "The API refused the request (HTTP {}). Check the API key."),
            429: ("rate_limited", "The API is rate limiting requests (HTTP {}).")}
    if resp["truncated"]:
        result.update(reason="too_large", message=f"The response was larger than {apiint.MAX_RESPONSE_SIZE:,} bytes.")
    elif 300 <= st < 400:
        result.update(reason="redirect", message=f"The API redirected (HTTP {st}); redirects are not followed.")
    elif st == 404 and ep.get("not_found_404"):
        result.update(status="not_found", reason="http_404", message="The API has no record of this (HTTP 404).")
    elif st == 404:
        result.update(reason="endpoint", message="The API endpoint was not found (HTTP 404).")
    elif st in msgs:
        result.update(reason=msgs[st][0], message=msgs[st][1].format(st))
    elif st >= 500:
        result.update(reason="server", message=f"The API had a server error (HTTP {st}).")
    elif st >= 400:
        result.update(reason="request", message=f"The API rejected the request (HTTP {st}).")
    else:
        try:
            data = json.loads(resp["content"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            result.update(reason="not_json", message="The API did not return JSON.")
            return result
        result["data"] = data
        if data in ({}, None):
            result.update(status="not_found", reason="empty", message="The API returned no data for this.")
        else:
            result.update(status="ok", reason="")
    return result


# ═════════════════════════════ formatting ═════════════════════════════

_LOVELACE = re.compile(r"\blovelaces?\b", re.I)
_UNIX = re.compile(r"unix (time|timestamp|epoch)|posix time|seconds since (the )?(unix )?epoch", re.I)
_IN_ADA = re.compile(r"\bin ada\b", re.I)
MAX_ROWS = 10
MAX_LINES = 12
MAX_SHOWN = 8


def unit_spec(description: str) -> Dict[str, Any]:
    """Display rules from a documented field description (units and timestamps)."""
    d = description or ""
    if _LOVELACE.search(d):
        return {"unit": "ADA", "scale": 6, "format": "number"}
    if _UNIX.search(d):
        return {"format": "timestamp"}
    if _IN_ADA.search(d):
        return {"unit": "ADA", "format": "number"}
    return {}


def humanize(path: str) -> str:
    last = re.split(r"[.\[\]]", path.strip("[]."))
    words = " ".join(w for w in last if w and not w.isdigit()).replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else path


def _is_amounts(value: Any) -> bool:
    return isinstance(value, list) and value and all(isinstance(v, dict) and "unit" in v and "quantity" in v for v in value)


def _amounts_text(value: list) -> str:
    ada = next((v for v in value if v.get("unit") == "lovelace"), None)
    others = [v for v in value if v.get("unit") != "lovelace"]
    parts = [apiint.format_value(ada["quantity"], {"scale": 6, "format": "number"}) + " ADA"] if ada else []
    if others:
        parts.append(f"{len(others)} other token{'s' if len(others) != 1 else ''}")
    return " + ".join(parts) or "none"


def _value_text(value: Any, spec: Mapping[str, Any], *, cell: bool = False) -> str:
    if _is_amounts(value):
        return _amounts_text(value)
    if isinstance(value, (dict, list)):
        return f"{len(value)} item{'s' if len(value) != 1 else ''}" if isinstance(value, list) else "…"
    text = apiint.format_value(value, spec)
    decimals = spec.get("decimals")
    if decimals is not None and value is not None and spec.get("format") != "timestamp":
        n = apiint._number(value)
        if n is not None:
            n = n.scaleb(-int(spec.get("scale") or 0))
            text = f"{n:,.{int(decimals)}f}"
    if cell and isinstance(value, str) and len(text) > 40:
        text = short(text)
    unit = spec.get("unit")
    return f"{text} {unit}" if unit and value is not None and text != "none" else text


def _auto_fields(sample: Any, schema: Optional[Mapping[str, Any]], *, table: bool) -> List[Dict[str, Any]]:
    """Fields to show when no override exists: documented order first, then the response's; scalars,
    amount lists and one level of nested objects; long ids last in tables."""
    props = (schema or {}).get("properties") or {}
    keys = list(props) + [k for k in (sample or {}) if k not in props] if isinstance(sample, dict) else list(props)
    out = []
    for k in keys:
        node = props.get(k) or {}
        v = sample.get(k) if isinstance(sample, dict) else None
        if isinstance(v, dict) and not table:
            sub = node.get("properties") or {}
            for k2, v2 in v.items():
                if not isinstance(v2, (dict, list)):
                    out.append({"path": f"{k}.{k2}", "label": humanize(f"{k} {k2}"), **unit_spec((sub.get(k2) or {}).get("description", ""))})
            continue
        if isinstance(v, list) and not _is_amounts(v):
            continue
        out.append({"path": k, "label": humanize(k), **unit_spec(node.get("description", ""))})
    if table:
        def weight(f):
            v = sample.get(f["path"]) if isinstance(sample, dict) else None
            long_id = isinstance(v, str) and len(v) > 40
            amount = _is_amounts(v) or bool(f.get("unit"))
            return (0 if f["path"] == "address" else 1 if amount else 3 if long_id else 2)
        out = sorted(out, key=weight)
    return out[:60]


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _zero(v: Any) -> bool:
    n = apiint._number(v) if isinstance(v, (int, float, str)) and not isinstance(v, bool) else None
    return n is not None and n == 0


def _field_concepts(f: Mapping[str, Any]) -> set:
    return _known(_concepts(f"{f['path'].replace('.', ' ')} {f.get('label') or ''}"))


def _table(rows: Sequence[Any], cols: Sequence[Mapping[str, Any]], channel: str, *, max_cols: int = 4) -> str:
    """Rows as a channel-suited table; columns empty in every shown row are dropped."""
    val = lambda r, c: apiint.get_path(r, c["path"]) if apiint.get_path(r, c["path"]) is not apiint._MISSING else None
    cols = [c for c in cols if any(not _empty(val(r, c)) for r in rows)][:max_cols] or list(cols)[:max_cols]
    cells = [[_value_text(val(r, c), c, cell=True) for c in cols] for r in rows]
    labels = [c.get("label") or humanize(c["path"]) for c in cols]
    if channel == "discord":
        widths = [max(len(labels[i]), *(len(row[i]) for row in cells)) for i in range(len(cols))]
        line = lambda vals: "  ".join(v.ljust(widths[i]) for i, v in enumerate(vals)).rstrip()
        return "```\n" + "\n".join([line(labels)] + [line(r) for r in cells]) + "\n```"
    if channel == "api":
        return "\n".join(["| " + " | ".join(labels) + " |", "|" + "---|" * len(labels)] + ["| " + " | ".join(r) + " |" for r in cells])
    return "\n".join("• " + " · ".join(f"{labels[i]}: {v}" for i, v in enumerate(r)) for r in cells)


def _rows(data: Any) -> Optional[list]:
    if isinstance(data, list):
        return data
    return None


def format_result(result: Mapping[str, Any], *, provider: str, endpoint: Mapping[str, Any],
                  fields: Optional[Sequence[Mapping[str, Any]]] = None, channel: str = "web",
                  target: Optional[Mapping[str, Any]] = None, question: str = "") -> Dict[str, Any]:
    """The text the tester shows and the bot receives, for ``channel`` web (plain text), discord
    (Markdown, tables in a code block) or api (Markdown with a pipe table).

    ``kind``: ``single``, ``list``, ``empty`` (the API answered with no rows: a genuine empty result),
    ``not_found`` (no match) or ``unavailable`` (couldn't check). Zero values are shown as values.

    Without an override, a single record shows the fields the ``question`` asks about first, then
    amounts and times, skipping empty values and zero counts the question didn't ask about; at
    most ``MAX_SHOWN`` lines. ``details`` holds every other field for the bot's follow-up answers.
    Lists inside a record (a transaction's inputs and outputs) are shown as tables."""
    md = channel in ("discord", "api")
    title = f"{provider} — {(endpoint.get('summary') or endpoint.get('path') or '').strip()}"
    about = f" for {short(target['value'])}" if target else ""
    head = (f"**{title}**" if md else title) + about
    status = result.get("status")
    if status == "not_found":
        return {"kind": "not_found", "text": f"{head}\nNo match: {provider} has no record of this"
                f"{' ' + KIND_LABELS.get(target['kind'], '') if target else ''}."}
    if status != "ok":
        return {"kind": "unavailable", "text": f"{head}\nLookup unavailable: {result.get('message') or 'the API could not be reached'} "
                "This says nothing about whether it exists."}
    data = result.get("data")
    schema = endpoint.get("response_schema") or {}
    rows = _rows(data)
    if rows is not None:
        if not rows:
            what = (endpoint.get("summary") or "records").strip().lower()
            return {"kind": "empty", "text": f"{head}\nNo {what} yet: {provider} answered with an empty list"
                                             f"{' for this ' + KIND_LABELS.get(target['kind'], '') if target else ''}."}
        item_schema = (schema.get("items") or {}) if schema else {}
        cols = list(fields) if fields else _auto_fields(rows[0] if isinstance(rows[0], dict) else {}, item_schema, table=True)
        shown = rows[:MAX_ROWS]
        more = f"Showing {len(shown)} of {len(rows)}." if len(rows) > len(shown) else f"{len(rows)} record{'s' if len(rows) != 1 else ''}."
        if not cols or not isinstance(rows[0], dict):
            body = "\n".join(f"• {_value_text(r, {}, cell=True)}" for r in shown)
            return {"kind": "list", "text": f"{head}\n{body}\n{more}", "details": ""}
        body = _table(shown, cols, channel, max_cols=len(cols) if fields else 4)
        return {"kind": "list", "text": f"{head}\n{body}\n{more}", "details": ""}
    if fields:                                   # an admin override: exactly these fields, in this order
        lines = []
        for f in fields:
            v = apiint.get_path(data, f["path"])
            if v is apiint._MISSING:
                continue
            label = f.get("label") or humanize(f["path"])
            val = _value_text(v, f)
            lines.append(f"• **{label}:** {val}" if md else f"{label}: {val}")
        body = "\n".join(lines) or json.dumps(data, ensure_ascii=False)[:300]
        return {"kind": "single", "text": f"{head}\n{body}", "details": ""}

    record = data if isinstance(data, dict) else {}
    q = _known(_concepts(question)) if question else set()
    qwords = {w.rstrip("s") for w in _WORD.findall(question.lower()) if len(w) > 3 and w not in _STOP} if question else set()
    named = lambda path: bool({w.rstrip("s") for w in re.split(r"[._]", path.lower()) if len(w) > 3} & qwords)
    # Lists inside the record (inputs/outputs…): tables, the one the question names first.
    props = (schema.get("properties") or {}) if schema else {}
    lists = [k for k, v in record.items() if isinstance(v, list) and v and isinstance(v[0], dict) and not _is_amounts(v)]
    asked_lists = [k for k in lists if named(k)] or [k for k in lists if _field_concepts({"path": k}) & q]
    show_lists = asked_lists or lists[:2]
    sections = []
    for k in show_lists:
        rows = record[k]
        cols = _auto_fields(rows[0], (props.get(k) or {}).get("items") or {}, table=True)
        more = f"Showing {MAX_ROWS} of {len(rows)}." if len(rows) > MAX_ROWS else f"{len(rows)} in total."
        sections.append(f"{humanize(k)}:\n{_table(rows[:MAX_ROWS], cols, channel)}\n{more}")

    flds = _auto_fields(record, schema, table=False)
    ranked = []
    for i, f in enumerate(flds):
        v = apiint.get_path(data, f["path"])
        if v is apiint._MISSING or (isinstance(v, list) and not _is_amounts(v)):
            continue
        if target and isinstance(v, str) and v.lower() == target["value"]:
            continue                      # the value the user asked about
        asked = named(f["path"]) or bool(_field_concepts(f) & q)
        if not asked and (_empty(v) or (_zero(v) and (f["path"].endswith("_count") or f.get("unit")))):
            continue                      # empty values and zero counts nobody asked about
        rank = 0 if asked else 1 if (f.get("unit") or f.get("format") == "timestamp" or _is_amounts(v)) else 2
        label = f.get("label") or humanize(f["path"])
        ranked.append((rank, i, label, _value_text(v, f, cell=True)))
    ranked.sort()
    limit = MAX_SHOWN if not sections else 4
    shown, rest = ranked[:limit], ranked[limit:]
    lines = [f"• **{l}:** {v}" if md else f"{l}: {v}" for _, _, l, v in shown]
    if asked_lists:
        lines = []                        # the question is about the list: show just that
        rest = ranked
    body = "\n".join(lines + sections) or json.dumps(data, ensure_ascii=False)[:300]
    details = "\n".join(f"{l}: {v}" for _, _, l, v in sorted(rest, key=lambda x: x[1]))
    return {"kind": "single", "text": f"{head}\n{body}", "details": details}


# ═════════════════════════════ structured UTxO records (spec 022) ═════════════════════════════
# One parse of the raw response: the bot's data, the Discord card and utxos.json all come from it,
# with full values (addresses, hashes, exact lovelace, policy ids, asset names in hex).

def _amounts_struct(amounts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    lovelace, assets = None, []
    for a in amounts:
        unit, qty = str(a.get("unit") or ""), str(a.get("quantity") or "0")
        if unit == "lovelace":
            lovelace = qty
        else:
            name_hex = unit[56:]
            try:
                name = bytes.fromhex(name_hex).decode("utf-8") if name_hex else ""
                name = name if name.isprintable() else None
            except ValueError:
                name = None
            assets.append({"policyId": unit[:56], "assetNameHex": name_hex, "assetName": name, "quantity": qty})
    return {"lovelace": lovelace, "assets": assets}


def _is_utxo(row: Any) -> bool:
    return isinstance(row, dict) and "output_index" in row and _is_amounts(row.get("amount"))


def _utxo(row: Mapping[str, Any], tx_hash: Optional[str] = None) -> Dict[str, Any]:
    out = {"txHash": row.get("tx_hash") or tx_hash, "outputIndex": row.get("output_index"), "address": row.get("address"),
           **_amounts_struct(row.get("amount") or [])}
    for key, name in (("block", "block"), ("tx_index", "txIndex"), ("data_hash", "dataHash"), ("inline_datum", "inlineDatum"),
                      ("reference_script_hash", "referenceScriptHash"), ("stake_address", "stakeAddress")):
        if row.get(key) is not None:
            out[name] = row[key]
    return out


def structured_result(data: Any) -> Optional[Dict[str, Any]]:
    """Exact UTxO records from a raw response: an address's UTxO list (``kind: utxos``) or a
    transaction's inputs and outputs (``kind: tx_utxos``). None for anything else."""
    if isinstance(data, list) and data and all(_is_utxo(r) for r in data):
        return {"kind": "utxos", "count": len(data), "utxos": [_utxo(r) for r in data]}
    if isinstance(data, dict) and any(isinstance(data.get(k), list) for k in ("inputs", "outputs")):
        ins, outs = data.get("inputs") or [], data.get("outputs") or []
        if all(_is_utxo(r) for r in ins + outs):
            return {"kind": "tx_utxos", "txHash": data.get("hash"),
                    "inputs": [_utxo(r) for r in ins], "outputs": [_utxo(r, tx_hash=data.get("hash")) for r in outs]}
    return None


def _ada(lovelace: Optional[str]) -> str:
    return apiint.format_value(lovelace, {"scale": 6, "format": "number"}) if lovelace is not None else "?"


def utxo_card(structured: Mapping[str, Any], *, provider: str, address: str, at: str, max_fields: int = 10) -> Dict[str, Any]:
    """A Discord card (embed fields as plain data) for an address's UTxOs, from ``structured``."""
    utxos = structured.get("utxos") or []
    total = sum(int(u["lovelace"]) for u in utxos if u.get("lovelace") and str(u["lovelace"]).isdigit())
    fields = []
    for i, u in enumerate(utxos[:max_fields]):
        lines = [f"`{u['txHash']}#{u['outputIndex']}`", f"{_ada(u.get('lovelace'))} ADA ({u.get('lovelace')} lovelace)"]
        for a in u["assets"][:5]:
            label = a["assetName"] if a.get("assetName") else (a["assetNameHex"] or "(no name)")
            lines.append(f"• {a['quantity']} × {label} · policy `{a['policyId']}`")
        if len(u["assets"]) > 5:
            lines.append(f"• …and {len(u['assets']) - 5} more assets (in utxos.json)")
        fields.append({"name": f"UTxO {i + 1}", "value": "\n".join(lines)[:1024]})
    more = f" · showing {max_fields}" if len(utxos) > max_fields else ""
    return {"title": f"{provider} — Address UTxOs",
            "description": f"`{address}`\n{len(utxos)} UTxO{'s' if len(utxos) != 1 else ''} · {_ada(str(total))} ADA in total{more}",
            "fields": fields, "footer": f"Every UTxO with exact values: utxos.json · looked up {at}"}


def utxos_json(structured: Mapping[str, Any], *, provider: str, operation: str, address: str, at: str) -> str:
    return json.dumps({"source": provider, "operation": operation, "address": address, "lookedUpAt": at,
                       "count": len(structured.get("utxos") or []), "utxos": structured.get("utxos") or []}, indent=2)


def documented_fields(endpoint: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Documented response fields (item fields for a list) with inferred display rules, for the
    formatting editor."""
    schema = endpoint.get("response_schema") or {}
    is_list = "array" in (schema.get("types") or [])
    node = (schema.get("items") or {}) if is_list else schema
    out = []
    for k, child in (node.get("properties") or {}).items():
        child = child or {}
        types = child.get("types") or []
        if "object" in types and child.get("properties"):
            for k2, c2 in child["properties"].items():
                out.append({"path": f"{k}.{k2}", "label": humanize(f"{k} {k2}"), "description": (c2 or {}).get("description", ""),
                            **unit_spec((c2 or {}).get("description", ""))})
            continue
        out.append({"path": k, "label": humanize(k), "description": child.get("description", ""),
                    **unit_spec(child.get("description", ""))})
    return out[:200]
