import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import current_user
from app.core.config import get_settings
from app.db.pool import get_pool
from app.schemas.contracts import (
    DecisionAnnotation,
    PerModuleBreakdown,
    RecommendationLogEntry,
    TargetPrice,
)

router = APIRouter(
    prefix="/recommendations", tags=["recommendations"], dependencies=[Depends(current_user)]
)


@router.get("/log", response_model=list[RecommendationLogEntry])
async def recommendation_log(
    ticker: str | None = Query(None),
    limit: int = Query(30, le=365),
) -> list[RecommendationLogEntry]:
    """Immutable recommendation history with any user annotations."""
    settings = get_settings()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.full_symbol, r.rec_date, r.composite_signal, r.composite_call,
                   r.target_price_bear, r.target_price_base, r.target_price_bull,
                   r.confidence_level, r.confidence_pct, r.conflict_flag,
                   r.reduced_confidence, r.horizon_months, r.per_module_breakdown,
                   r.data_completeness, r.methodology_version,
                   d.decision,
                   CASE WHEN d.transaction_price_enc IS NULL THEN NULL
                        ELSE pgp_sym_decrypt(d.transaction_price_enc, $3) END AS price_txt,
                   CASE WHEN d.notes_enc IS NULL THEN NULL
                        ELSE pgp_sym_decrypt(d.notes_enc, $3) END AS notes_txt
            FROM recommendation r
            JOIN ticker t ON t.id = r.ticker_id
            LEFT JOIN LATERAL (
                SELECT * FROM user_decision_log d
                WHERE d.ticker_id = r.ticker_id AND d.recommendation_date = r.rec_date
                ORDER BY d.logged_at DESC LIMIT 1
            ) d ON TRUE
            WHERE ($1::text IS NULL OR upper(t.full_symbol) = upper($1))
            ORDER BY r.rec_date DESC
            LIMIT $2
            """,
            ticker, limit, settings.app_encryption_key,
        )
    entries: list[RecommendationLogEntry] = []
    for r in rows:
        conf_pct = float(r["confidence_pct"]) if r["confidence_pct"] is not None else None
        annotation = None
        if r["decision"] is not None:
            annotation = DecisionAnnotation(
                decision=r["decision"],
                transaction_price=float(r["price_txt"]) if r["price_txt"] else None,  # noqa
                notes=r["notes_txt"],
            )
        target = None
        if r["target_price_base"] is not None:
            target = TargetPrice(
                bear=float(r["target_price_bear"]) if r["target_price_bear"] is not None else None,
                base=float(r["target_price_base"]),
                bull=float(r["target_price_bull"]) if r["target_price_bull"] is not None else None,
            )
        signal = r["composite_signal"]
        entries.append(
            RecommendationLogEntry(
                ticker=r["full_symbol"],
                rec_date=r["rec_date"],
                composite_signal=float(signal) if signal is not None else None,
                composite_call=r["composite_call"],
                target_price=target,
                confidence_level=r["confidence_level"],
                confidence_pct=conf_pct,
                conflict_flag=r["conflict_flag"],
                reduced_confidence=r["reduced_confidence"],
                horizon_months=r["horizon_months"],
                data_completeness=float(r["data_completeness"]),
                methodology_version=r["methodology_version"],
                per_module_breakdown=[
                    PerModuleBreakdown(**item) for item in json.loads(r["per_module_breakdown"])
                ],
                annotation=annotation,
            )
        )
    return entries


@router.post("/log/{rec_date}/annotate", status_code=status.HTTP_201_CREATED)
async def annotate(
    rec_date: date,
    body: DecisionAnnotation,
    ticker: str = Query(...),
) -> dict[str, str]:
    """Records what the user did. Annotates the immutable recommendation row —
    never mutates it. Price/notes are personal financial data: encrypted
    column-level via pgcrypto before hitting disk (NFR-05)."""
    settings = get_settings()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_decision_log
                (ticker_id, recommendation_date, decision, transaction_price_enc, notes_enc)
            SELECT t.id, $2, $3,
                   CASE WHEN $4::text IS NULL THEN NULL ELSE pgp_sym_encrypt($4::text, $6) END,
                   CASE WHEN $5::text IS NULL THEN NULL ELSE pgp_sym_encrypt($5::text, $6) END
            FROM ticker t
            JOIN recommendation r ON r.ticker_id = t.id AND r.rec_date = $2
            WHERE upper(t.full_symbol) = upper($1)
            RETURNING id
            """,
            ticker, rec_date, body.decision,
            str(body.transaction_price) if body.transaction_price is not None else None,
            body.notes, settings.app_encryption_key,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SECTOR_NOT_COVERED",
                    "message": "No recommendation exists for that ticker/date."},
        )
    return {"status": "created"}
