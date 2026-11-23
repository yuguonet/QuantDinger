"""GBDT tail-scores tool exposed to the agent tool face."""
from __future__ import annotations

from ...log import logger
from ...wheels.gbdt_tail.score import compute_scores


def get_tail_scores(date: str = "", k: int = 200, universe: str = "", detail: str = "compact") -> dict:
    """Get daily GBDT coarse-screen top-K tail scores from local DB (read-only).

    Args:
        date: target trading day YYYY-MM-DD; empty means the latest available day
        k: number of top-ranked stocks to return
        universe: optional comma-separated board filter among main,gem,star,bse
        detail: "compact" returns rank/code/score/tradable; "full" returns all row fields

    Returns:
        dict: as_of, top_k, rows[compact or full],
            meta{model_ver,caliber,universe,missing,cache_hit,error}
    """
    try:
        result = compute_scores(as_of=date or None, top_k=k, universe=universe)
        if str(detail).lower() != "full":
            result["rows"] = [{key: row[key] for key in ("rank", "code", "score", "tradable")}
                              for row in result["rows"]]
        return result
    except Exception as exc:
        logger.warning("[get_tail_scores] failed: %s", exc)
        return {"as_of": date, "top_k": k, "rows": [],
                "meta": {"error": f"{type(exc).__name__}: {exc}"}}
