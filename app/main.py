"""
Demo target app: a minimal e-commerce order API.
Contains a seeded bug: integer division by zero when discount=100.
The self-healing system will detect, diagnose, reproduce, patch, and redeploy it.
"""
import os
import time
import random
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="OrderService", version="1.0.0")

# Simulated in-memory order store
orders: dict[int, dict] = {}
order_counter = 0


class OrderRequest(BaseModel):
    item: str
    quantity: int
    price_cents: int
    discount_pct: int = 0  # 0-99 normally, but user can send 100 → crashes


class OrderResponse(BaseModel):
    order_id: int
    item: str
    quantity: int
    total_cents: int
    discount_applied: int


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/metrics")
def metrics():
    return {
        "total_orders": len(orders),
        "uptime_seconds": time.time() - _start_time,
    }


@app.post("/orders", response_model=OrderResponse)
def create_order(req: OrderRequest):
    global order_counter

    # BUG: ZeroDivisionError when discount_pct == 100
    # Should be: total = req.price_cents * req.quantity * (100 - req.discount_pct) // 100
    multiplier = 100 // (100 - req.discount_pct)  # <-- crashes at discount_pct=100
    total_cents = req.price_cents * req.quantity // multiplier

    order_counter += 1
    order_id = order_counter
    orders[order_id] = {
        "item": req.item,
        "quantity": req.quantity,
        "total_cents": total_cents,
        "discount_applied": req.discount_pct,
    }

    logger.info(f"Order {order_id} created: {req.item} x{req.quantity} = {total_cents}¢")
    return OrderResponse(order_id=order_id, **orders[order_id])


@app.get("/orders/{order_id}", response_model=OrderResponse)
def get_order(order_id: int):
    if order_id not in orders:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(order_id=order_id, **orders[order_id])


@app.post("/trigger-bug")
def trigger_bug():
    """Convenience endpoint to fire the seeded bug for demo purposes."""
    try:
        create_order(OrderRequest(item="demo", quantity=1, price_cents=1000, discount_pct=100))
    except ZeroDivisionError as e:
        logger.error(f"CRITICAL: ZeroDivisionError in create_order: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


_start_time = time.time()
