"""Shop API — a small demo service for Cicatrixa deploys."""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Shop API")

_items: dict[int, dict] = {}
_next_id = 1


class ItemIn(BaseModel):
    name: str
    price: float


@app.get("/")
def index():
    return {
        "service": "shop-api",
        "endpoints": ["/items (GET, POST)", "/items/{id}", "/stats", "/health"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/items")
def create_item(item: ItemIn):
    global _next_id
    item_id = _next_id
    _next_id += 1
    _items[item_id] = {"id": item_id, "name": item.name, "price": item.price}
    return _items[item_id]


@app.get("/items")
def list_items():
    return list(_items.values())


@app.get("/items/{item_id}")
def get_item(item_id: int):
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="item not found")
    return _items[item_id]


@app.get("/stats")
def stats():
    prices = [i["price"] for i in _items.values()]
    return {
        "count": len(prices),
        "total": sum(prices),
        "average": sum(prices) / len(prices),
        "max": max(prices, default=0),
    }
