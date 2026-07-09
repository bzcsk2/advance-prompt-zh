from fastapi import FastAPI
from pydantic import BaseModel

from agentic_rag_enterprise.graph.runtime import AgenticRagRuntime

app = FastAPI(title="agentic-rag-enterprise", version="0.1.0")
runtime = AgenticRagRuntime()


class ChatRequest(BaseModel):
    query: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
def chat(request: ChatRequest) -> dict:
    state = runtime.run(request.query)
    return state.model_dump()
