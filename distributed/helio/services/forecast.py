import statistics
import time
from urllib.parse import urlencode
from fastapi import Request, Query
from helio.common import actor, rpc, service_app, stamp, uid


def linear(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0
    return lambda x: max(0, my + slope * (x - mx))


def predict(points, horizon):
    if len(points) < 12:
        return {
            "status": "insufficient_data",
            "samples": len(points),
            "message": "Collect at least 12 observations before training and chronological evaluation.",
            "points": points,
        }
    if time.time() - points[-1]["occurred"] > 30:
        return {
            "status": "stale",
            "samples": len(points),
            "message": "Fresh observations are required.",
            "points": points,
        }
    origin = points[0]["occurred"]
    xs = [p["occurred"] - origin for p in points]
    ys = [p["value"] for p in points]
    train = max(6, len(points) // 2)
    test = max(train + 2, int(len(points) * 0.75))

    def estimate(kind, end, x):
        if kind == "Linear trend":
            return linear(xs[:end], ys[:end])(x)
        if kind == "Previous observation":
            return ys[end - 1]
        mean = ys[0]
        for y in ys[1:end]:
            mean = 0.3 * y + 0.7 * mean
        return mean

    candidates = {
        kind: statistics.mean(abs(ys[i] - estimate(kind, i, xs[i])) for i in range(train, test))
        for kind in ("Linear trend", "Previous observation", "Exponential smoothing")
    }
    model = min(candidates, key=candidates.get)
    comparisons = []
    for i in range(test, len(points)):
        comparisons.append(
            {
                "time": stamp(points[i]["occurred"]),
                "actual": ys[i],
                "predicted": round(estimate(model, i, xs[i]), 3),
                "baseline": ys[i - 1],
            }
        )
    errors = [abs(r["actual"] - r["predicted"]) for r in comparisons]
    percentages = [
        abs((r["actual"] - r["predicted"]) / r["actual"]) * 100 for r in comparisons if r["actual"]
    ]
    future = [
        {
            "occurred": points[-1]["occurred"] + horizon * i / 12,
            "value": round(estimate(model, len(points), xs[-1] + horizon * i / 12), 3),
        }
        for i in range(13)
    ]
    return {
        "status": "ready",
        "model": model,
        "samples": len(points),
        "horizon_seconds": horizon,
        "expected": future[-1]["value"],
        "mae": round(statistics.mean(errors), 3),
        "baseline_mae": round(
            statistics.mean(abs(r["actual"] - r["baseline"]) for r in comparisons), 3
        ),
        "mape": round(statistics.mean(percentages), 2) if percentages else None,
        "mape_samples": len(percentages),
        "evaluated_samples": len(comparisons),
        "validation": "Model selected on earlier validation segment; reported errors use the later chronological test segment. Walk-forward fits use past data only.",
        "candidate_validation_mae": candidates,
        "last_observation": stamp(points[-1]["occurred"]),
        "points": points,
        "evaluation": comparisons,
        "forecast": future,
        "residual_band": [None, None],
        "band_description": "No calibrated probability interval is claimed.",
    }


def install(app):
    @app.get("/api/predictions")
    def forecast(
        request: Request,
        service_id: str = "svc-worker",
        horizon_seconds: int = Query(60, ge=10, le=300),
    ):
        actor(request)
        points = rpc(
            "metrics",
            "/internal/history?"
            + urlencode({"service_id": service_id, "metric_name": "request_rate"}),
        )["points"][-180:]
        result = predict(points, horizon_seconds)
        if result["status"] == "ready":
            app.state.ctx.db.put(
                "evaluations",
                service_id,
                {k: v for k, v in result.items() if k not in {"points", "forecast"}},
            )
        return result


app = service_app("forecast", install)
