import json
import math

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import current_user
from app.db.pool import get_pool
from app.schemas.contracts import ModuleWeights, WeightConfig

router = APIRouter(prefix="/config", tags=["config"], dependencies=[Depends(current_user)])


@router.get("/weights", response_model=WeightConfig)
async def get_weights() -> WeightConfig:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT module_weights, horizon_months FROM user_config WHERE id = 1"
        )
    return WeightConfig(
        module_weights=ModuleWeights(**json.loads(row["module_weights"])),
        horizon_months=row["horizon_months"],
    )


@router.put("/weights", response_model=WeightConfig)
async def put_weights(body: WeightConfig) -> WeightConfig:
    weights = body.module_weights.model_dump()
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-3):
        # openapi.yaml: weights must sum to 1.0 (±0.001), non-negative.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": "Weights must sum to 1.0 (±0.001)."},
        )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_config SET module_weights = $1, horizon_months = $2,"
            " updated_at = now() WHERE id = 1",
            json.dumps(weights), body.horizon_months,
        )
    return body
