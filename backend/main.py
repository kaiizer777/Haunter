from fastapi import FastAPI

app = FastAPI(title="Haunter Backend")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
