from fastapi import FastAPI

app = FastAPI(title="Nyaya Bandhu")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"service": "nyaya-bandhu", "bootstrapping": True}
