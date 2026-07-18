# Shop API

A small FastAPI service. Run with:

```
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Endpoints

- `GET /` — service info
- `GET /health` — health check
- `POST /items` — add an item: `{"name": "book", "price": 12.5}`
- `GET /items` — list items
- `GET /items/{id}` — fetch one item
- `GET /stats` — price statistics across all items
