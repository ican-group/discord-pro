"""BitBotPro core foundation (spec 012, Phase 2).

Enterprise -> Organisation -> Bot -> Bot Instance, one configuration registry,
one resolver, one lifecycle model, one permission model, credential ownership
and the knowledge-control contract.

Isolation rule (Phase 2): this package is pure Python (standard library plus the
optional ``cryptography`` package for secret encryption). It must not import the
current application (``server``, routers, Motor, FastAPI), and the current
application must not import it until a later, approved phase wires it in.
``tests/unit/test_core_isolation.py`` enforces both directions.
"""
