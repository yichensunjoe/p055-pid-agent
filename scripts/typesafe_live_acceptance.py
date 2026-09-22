"""Live acceptance for the TypeSafe drawing path: a real key, real judgments, a real compiler.

The unit tests inject a transport, which is what makes them hermetic -- and which is also why they
cannot tell a working credential from a typo, or a plausible-looking criteria dictionary from one
System One actually understands. This script does the other half, against the real service:

1. **credential** -- ``verify()`` asks one judgment and reports the model and latency;
2. **candidates** -- a scratch document with two drawn devices, so code has something to offer;
3. **drawing** -- one sentence with an add clause and a connect clause goes through the real planner:
   every judgment's choice and confidence is printed, and the resulting transaction is compiled by
   the same permissive compiler the API uses;
4. **verdict** -- exit 0 only when a judgment selected each clause's candidate *and* the transaction
   compiles valid. A skipped clause (below the confidence floor) or a compile issue is a failure of
   this check, never something to average away.

The key is read from the environment exactly like the server does (``TYPESAFE_API_KEY``,
``TYPESAFE_BASE_URL``); it is never printed, and the report says only that it was present.

    TYPESAFE_API_KEY=... .venv/bin/python scripts/typesafe_live_acceptance.py
    # a shell that keeps it in ~/.zshrc (not inherited by non-interactive shells):
    eval "$(grep -E '^[[:space:]]*(export[[:space:]]+)?TYPESAFE_[A-Z_]+=' ~/.zshrc | sed -E 's/^[[:space:]]*export[[:space:]]+//' | tr -d '"')"
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agentcad.models import (  # noqa: E402
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
)
from agentcad.permissive_semantic_compiler import (  # noqa: E402
    PermissiveSemanticTransactionCompiler,
)
from agentcad.service import DocumentService  # noqa: E402
from agentcad.store import SQLiteDocumentStore  # noqa: E402
from agentcad.symbols import SymbolRegistry  # noqa: E402
from agentcad.typesafe import TypesafeClient, TypesafeError, resolve_typesafe_config  # noqa: E402
from agentcad.typesafe_planner import TypesafeSemanticPlanner  # noqa: E402

PROMPT = "新增一台离心泵 P-201，把 T-101 接到 T-102"
SEED_SYMBOL = "centrifugal_pump"
SEED_LABELS = ("T-101", "T-102")


@dataclass(frozen=True)
class PlanInput:
    prompt: str
    typesafe_config: Any
    expected_revision: int | None = None


def _seed(service: DocumentService, document_id: str, label: str, x: float) -> None:
    definition = service.symbols.get(SEED_SYMBOL)
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id=f"sym_{label}",
                        symbol_key=definition.key,
                        position=Point(x=x, y=120),
                        width=definition.width,
                        height=definition.height,
                        label=label,
                        properties={"tag": label},
                    )
                )
            ],
            label="live-acceptance seed",
        ),
        source="system",
    )


def main() -> int:
    try:
        config = resolve_typesafe_config()
    except TypesafeError as error:
        print(f"FAIL {error.code}: {error.message}")
        return 2
    print(f"credential: {config.describe()}")
    client = TypesafeClient(config)

    verification = client.verify()
    print(
        f"verify: ok model={verification['model']} latency={verification['latency_ms']}ms "
        f"answer={json.dumps(verification['answer'])}"
    )

    with tempfile.TemporaryDirectory() as tmp:
        service = DocumentService(SQLiteDocumentStore(Path(tmp) / "live.db"), SymbolRegistry())
        document = service.create_document(
            CreateDocumentRequest(name="typesafe live acceptance"), source="system"
        )
        for index, label in enumerate(SEED_LABELS):
            _seed(service, document.id, label, 100.0 + index * 220.0)

        planner = TypesafeSemanticPlanner(service, service.symbols)
        try:
            plan = planner.plan(
                document.id,
                PlanInput(prompt=PROMPT, typesafe_config=config, expected_revision=None),
            )
        except TypesafeError as error:
            print(f"FAIL {error.code}: {error.message}")
            return 1

        print("plan:", plan.explanation)
        operations = plan.transaction.operations
        for operation in operations:
            print("  -", json.dumps(operation.model_dump(mode="json"), ensure_ascii=False)[:200])

        compiled = PermissiveSemanticTransactionCompiler(service).compile(document.id, plan.transaction)
        issues = [issue.code for issue in compiled.assessment.issues]
        print(
            f"compile: valid={compiled.assessment.valid} stage={compiled.assessment.stage} "
            f"issues={issues or 'none'}"
        )

    selected = len(operations) >= 2
    skipped = "[跳过]" in plan.explanation
    valid = compiled.assessment.valid and not issues
    ok = selected and valid and not skipped
    print(
        "verdict: "
        + (
            "PASS — 每个子句都被判读选中，事务通过编译"
            if ok
            else f"FAIL — selected={selected} skipped_clause={skipped} compile_valid={valid}"
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
