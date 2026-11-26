"""MiniOnto — 팔란티어식 온톨로지의 핵심 개념을 재현하는 교육·연구용 경량 엔진"""

from .loader import OntologyLoadError, load
from .metamodel import Ontology
from .schema import Schema
from .validator import Report, validate

__all__ = ["Ontology", "OntologyLoadError", "Report", "Schema", "load", "validate"]
__version__ = "0.1.0"
