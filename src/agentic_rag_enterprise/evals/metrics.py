from pydantic import BaseModel


class EvalResult(BaseModel):
    name: str
    score: float
    details: dict = {}


def citation_coverage(answer_citations: list[str], required_evidence_ids: list[str]) -> EvalResult:
    """Measure whether required evidence ids appear in the answer citation map."""
    if not required_evidence_ids:
        return EvalResult(name="citation_coverage", score=1.0, details={"reason": "no required ids"})

    covered = set(answer_citations) & set(required_evidence_ids)
    score = len(covered) / len(set(required_evidence_ids))
    return EvalResult(
        name="citation_coverage",
        score=score,
        details={"covered": sorted(covered), "required": required_evidence_ids},
    )
