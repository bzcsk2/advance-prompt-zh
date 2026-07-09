from agentic_rag_enterprise.graph.runtime import AgenticRagRuntime
from agentic_rag_enterprise.schemas import SufficiencyStatus


def test_runtime_returns_grounded_answer_shape() -> None:
    runtime = AgenticRagRuntime()
    state = runtime.run("What is Agentic RAG?")

    assert state.plan is not None
    assert state.evidence
    assert state.sufficiency_decisions
    assert state.sufficiency_decisions[-1].status == SufficiencyStatus.SUFFICIENT
    assert state.final_answer is not None
    assert state.final_answer.citations
    assert state.stop_reason == "sufficient_context"
