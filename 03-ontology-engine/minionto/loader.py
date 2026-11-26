"""YAML 파일 → Ontology 객체"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .metamodel import Ontology


class OntologyLoadError(Exception):
    def __init__(self, path: Path, problems: list[str]):
        self.path, self.problems = path, problems
        super().__init__(f"{path}: 정의 오류 {len(problems)}건\n" + "\n".join(f"  - {p}" for p in problems))


def load(path: str | Path) -> Ontology:
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    try:
        return Ontology.model_validate(raw)
    except ValidationError as e:
        problems = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            problems.append(f"{loc}: {err['msg']}")
        raise OntologyLoadError(path, problems) from None
