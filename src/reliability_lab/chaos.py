from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider

# Typical token count per request for cost-saved estimation when cache hits.
_AVG_TOKENS_PER_REQUEST = 50


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive average recovery time from circuit breaker transition logs."""
    recovery_times: list[float] = []

    for breaker in gateway.breakers.values():
        opened_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open":
                opened_ts = float(entry["ts"])
            elif entry["to"] == "closed" and opened_ts is not None:
                recovery_times.append((float(entry["ts"]) - opened_ts) * 1000)
                opened_ts = None

    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _primary_cost_per_request(config: LabConfig) -> float:
    if not config.providers:
        return 0.001
    primary = config.providers[0]
    return (_AVG_TOKENS_PER_REQUEST / 1000.0) * primary.cost_per_1k_tokens


def _accumulate_result(metrics: RunMetrics, result: GatewayResponse, config: LabConfig) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost

    if result.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += _primary_cost_per_request(config)

    if result.route == "fallback":
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif result.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1

    if result.latency_ms > 0:
        metrics.latencies_ms.append(result.latency_ms)


def evaluate_scenario_pass(name: str, metrics: RunMetrics) -> tuple[bool, str]:
    """Scenario-specific pass/fail criteria with human-readable reason."""
    if name == "primary_timeout_100":
        passed = metrics.availability >= 0.95 and metrics.fallback_success_rate >= 0.9
        reason = (
            f"availability={metrics.availability:.2%} (need >=95%), "
            f"fallback_success_rate={metrics.fallback_success_rate:.2%} (need >=90%)"
        )
    elif name == "primary_flaky_50":
        passed = metrics.availability >= 0.75 and metrics.circuit_open_count >= 1
        reason = (
            f"availability={metrics.availability:.2%} (need >=75%), "
            f"circuit_opens={metrics.circuit_open_count} (need >=1)"
        )
    elif name == "all_healthy":
        passed = metrics.availability >= 0.90
        reason = f"availability={metrics.availability:.2%} (need >=90%)"
    else:
        passed = metrics.successful_requests > 0
        reason = "default: at least one successful request"
    return passed, reason


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario (sequential or concurrent)."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    prompts = [random.choice(queries) for _ in range(config.load_test.requests)]
    workers = config.load_test.concurrent_workers

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(gateway.complete, prompts))
        for result in results:
            _accumulate_result(metrics, result, config)
    else:
        for prompt in prompts:
            _accumulate_result(metrics, gateway.complete(prompt), config)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for entry in breaker.transition_log
        if entry["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def run_cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    """Run baseline scenario with cache disabled vs enabled for A/B comparison."""
    baseline = ScenarioConfig(
        name="cache_comparison",
        description="Healthy providers — compare latency/cost with and without cache",
        provider_overrides={},
    )

    no_cache_config = config.model_copy(deep=True)
    no_cache_config.cache.enabled = False
    without = run_scenario(no_cache_config, queries, baseline)

    with_cache_config = config.model_copy(deep=True)
    with_cache_config.cache.enabled = True
    with_cache = run_scenario(with_cache_config, queries, baseline)

    return {
        "without_cache": without.to_report_dict(),
        "with_cache": with_cache.to_report_dict(),
    }


def run_simulation(config: LabConfig, queries: list[str]) -> tuple[RunMetrics, dict[str, object]]:
    """Run all named scenarios and return combined metrics plus extended report data."""
    scenario_details: dict[str, object] = {}

    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        passed, reason = evaluate_scenario_pass("default", metrics)
        metrics.scenarios = {"default": "pass" if passed else "fail"}
        scenario_details["default"] = {"status": metrics.scenarios["default"], "reason": reason}
        extended = {
            "scenario_details": scenario_details,
            "cache_comparison": run_cache_comparison(config, queries),
            "concurrent_workers": config.load_test.concurrent_workers,
            "cache_backend": config.cache.backend if config.cache.enabled else "disabled",
        }
        return metrics, extended

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        passed, reason = evaluate_scenario_pass(scenario.name, result)
        status = "pass" if passed else "fail"
        combined.scenarios[scenario.name] = status
        scenario_details[scenario.name] = {
            "status": status,
            "reason": reason,
            "description": scenario.description,
            "availability": round(result.availability, 4),
            "fallback_success_rate": round(result.fallback_success_rate, 4),
            "cache_hit_rate": round(result.cache_hit_rate, 4),
            "circuit_open_count": result.circuit_open_count,
            "recovery_time_ms": result.recovery_time_ms,
        }

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    extended = {
        "scenario_details": scenario_details,
        "cache_comparison": run_cache_comparison(config, queries),
        "concurrent_workers": config.load_test.concurrent_workers,
        "cache_backend": config.cache.backend if config.cache.enabled else "disabled",
    }
    return combined, extended


def write_full_report(metrics: RunMetrics, extended: dict[str, object], json_path: str | Path) -> None:
    """Write combined metrics JSON including scenario details and cache comparison."""
    payload = metrics.to_report_dict()
    payload.update(extended)
    path = Path(json_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
