"""M10 runtime package — domain-neutral harness runtime primitives and ports.

P1 scope (Gate M10-P1 CODE GO): this package holds ONLY the neutral primitives
(StrictModel / utc_now) and the contract protocols (ports) the harness/domain
seam will use from P2 onward. It must stay importable without any P&ID module;
``test_m10_p1_runtime.py`` locks that with a subprocess isolation assertion.

This ``__init__`` is deliberately empty: nothing here may eager-import future
harness or domain modules (that would recreate the cycles this extraction
exists to break).
"""
