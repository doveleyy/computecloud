from fastapi import FastAPI, status

app = FastAPI(title="Personal Compute Cloud")

@app.get("/health", status_code= status.HTTP_200_OK)
def health() -> dict[str, str]:
    return {"status": "healthy"}

@app.get("/version")
def version() -> dict[str, str]:
    return {
        "name": "personal-compute-cloud",
        "version": "0.1.0"
        }