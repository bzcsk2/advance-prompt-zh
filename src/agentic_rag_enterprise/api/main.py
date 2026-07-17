from fastapi import FastAPI
from pydantic import BaseModel

from agentic_rag_enterprise.api.routes.chat import chat_v1
from agentic_rag_enterprise.api.schemas import ChatResponse
from agentic_rag_enterprise.graph.runtime import AgenticRagRuntime

app = FastAPI(title="agentic-rag-enterprise", version="0.1.0")
runtime = AgenticRagRuntime()

# E-014: synchronous enterprise chat endpoint (Fast Path + AnswerEnvelope).
# Registered with @app.post because include_router is a no-op in this env.
app.post("/v1/chat", response_model=ChatResponse)(chat_v1)


class ChatRequest(BaseModel):
    query: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
def chat(request: ChatRequest) -> dict:
    # Legacy M0 baseline endpoint (graph mock). Retained for characterization
    # tests; the enterprise path is POST /v1/chat.
    state = runtime.run(request.query)
    return state.model_dump()
