# Day 10 Lab — Reliability Engineering for Production Agents

| | |
|---|---|
| **Họ và tên** | Nguyễn Văn Chung |
| **Mã sinh viên** | 2A202600647 |
| **Track** | Phase 2 — Track 3 — Day 10 |
| **Đề tài** | Reliability layer cho LLM agent gateway |
| **Trạng thái** | Hoàn thành |

---

## Tổng quan

Project xây dựng **lớp reliability kiểu production** cho một LLM agent gateway: circuit breaker 3 trạng thái, semantic cache (in-memory + Redis), chuỗi fallback provider, chaos testing và observability metrics.

Toàn bộ logic reliability được implement trong `src/reliability_lab/`. Provider dùng `FakeLLMProvider` — **không cần API key thật**, mô phỏng latency, failure rate và cost cục bộ.

### Mục tiêu đạt được

1. Circuit breaker **CLOSED → OPEN → HALF_OPEN → CLOSED** với transition log.
2. Pipeline gateway: **cache → circuit breaker → provider fallback → static fallback**.
3. Semantic cache với **n-gram cosine similarity**, privacy guardrails và false-hit detection.
4. **Shared Redis cache** cho multi-instance deployment (Docker).
5. Chaos scenarios (3+) với pass/fail criteria, concurrent load, cache A/B comparison.
6. Metrics **JSON + CSV** và báo cáo `final_report.md` dựa trên số liệu thực tế.

---

## Kiến trúc hệ thống

```
User Request
    |
    v
[ReliabilityGateway]
    |
    +---> [SharedRedisCache.get()] -- HIT? --> return (route=cache_hit:score, cost=0)
    |              |
    |              v MISS
    +---> [CircuitBreaker: primary] --> FakeLLMProvider(primary)
    |         | OPEN / ProviderError
    |         v
    +---> [CircuitBreaker: backup]  --> FakeLLMProvider(backup)
    |         | all failed
    |         v
    +---> [Static fallback message]
```

| Layer | File | Mô tả |
|---|---|---|
| Gateway | `gateway.py` | Điều phối cache → breaker → fallback chain |
| Circuit breaker | `circuit_breaker.py` | State machine 3 trạng thái, fail-fast khi OPEN |
| Cache | `cache.py` | `ResponseCache` (in-memory) + `SharedRedisCache` (Redis HSET/SCAN) |
| Chaos | `chaos.py` | Scenarios, concurrent load, pass/fail criteria, cache comparison |
| Metrics | `metrics.py` | P50/P95/P99, availability, JSON/CSV export |
| Config | `config.py` | Pydantic loader từ YAML |
| Providers | `providers.py` | Fake LLM — latency, failure, cost simulation |

---

## Cấu hình chính (`configs/default.yaml`)

| Tham số | Giá trị | Lý do |
|---|---:|---|
| `failure_threshold` | 3 | Tránh circuit flip quá nhạy với lỗi ngẫu nhiên |
| `reset_timeout_seconds` | 2 | Probe HALF_OPEN nhanh, phù hợp lab |
| `success_threshold` | 1 | Một probe thành công đủ để đóng circuit |
| `cache.ttl_seconds` | 300 | Cân bằng freshness vs hit rate |
| `cache.similarity_threshold` | 0.92 | Kết hợp false-hit guard cho query có năm/ID khác nhau |
| `cache.backend` | `redis` | Shared state giữa nhiều gateway instance |
| `load_test.requests` | 100 | Đủ mẫu cho percentile ổn định |
| `load_test.concurrent_workers` | 4 | Load song song, stress circuit breaker |

### Chaos scenarios

| Scenario | Mô tả | Tiêu chí pass |
|---|---|---|
| `primary_timeout_100` | Primary fail 100% → fallback backup | availability ≥ 95%, fallback_rate ≥ 90% |
| `primary_flaky_50` | Primary fail 50% → circuit oscillate | availability ≥ 75%, circuit_opens ≥ 1 |
| `all_healthy` | Baseline cả hai provider healthy | availability ≥ 90% |

---

## Kết quả chạy thực tế

Nguồn: `reports/metrics.json` — sinh bởi `make run-chaos` (Redis backend, 4 workers).

| Metric | Giá trị |
|---|---:|
| total_requests | 300 |
| availability | 99.00% |
| error_rate | 1.00% |
| latency_p50_ms | 289.14 |
| latency_p95_ms | 318.39 |
| latency_p99_ms | 319.59 |
| fallback_success_rate | 95.38% |
| cache_hit_rate | 71.67% |
| circuit_open_count | 4 |
| estimated_cost_saved | 0.1075 |

**Tất cả 3 chaos scenarios: pass.**

Chi tiết đầy đủ: [`reports/final_report.md`](reports/final_report.md)

---

## Cài đặt và chạy

### Yêu cầu

- Python ≥ 3.10
- Docker (cho Redis shared cache)
- `pip install -e ".[dev]"`

### Quickstart

```bash
# Cài dependencies
pip install -e ".[dev]"

# Khởi động Redis
make docker-up

# Chạy toàn bộ test (cần Redis đang chạy)
make test

# Chạy chaos simulation → metrics.json + metrics.csv
make run-chaos

# (Tuỳ chọn) Tạo report tự động từ metrics
make report
```

### Windows — lưu ý pytest

Nếu pytest lỗi do plugin global (`deepeval`), chạy:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest tests/ -v
```

### Kiểm tra Redis shared cache

```bash
# Test shared state giữa 2 instance
pytest tests/test_redis_cache.py::test_shared_state_across_instances -v

# Xem keys trong Redis sau chaos run
docker compose exec redis redis-cli KEYS "rl:cache:*"
```

---

## Cấu trúc repository

```
src/reliability_lab/
  circuit_breaker.py   # State machine CLOSED / OPEN / HALF_OPEN
  cache.py             # ResponseCache + SharedRedisCache
  gateway.py           # Pipeline cache → breaker → fallback
  chaos.py             # Scenarios, concurrent load, cache A/B, pass/fail
  metrics.py           # RunMetrics, percentile, JSON/CSV export
  providers.py         # FakeLLMProvider
  config.py            # Pydantic config loader

configs/
  default.yaml         # Providers, CB, cache, scenarios, load test

scripts/
  run_chaos.py         # CLI chaos → reports/metrics.json + .csv
  generate_report.py   # CLI tạo report từ metrics JSON

tests/
  test_circuit_breaker.py    # 12 tests
  test_cache.py              # 9 tests
  test_gateway_contract.py   # 4 tests
  test_redis_cache.py        # 6 tests (cần Redis)
  test_todo_requirements.py  # 7 xfail → XPASS khi hoàn thành
  test_config.py             # 2 tests
  test_metrics.py            # 2 tests

data/
  sample_queries.jsonl       # 20 queries (privacy, technical, faq, dated)

reports/
  metrics.json               # Metrics + scenario details + cache comparison
  metrics.csv                # CSV export một dòng
  final_report.md            # Báo cáo đầy đủ (architecture, SLO, Redis evidence)
  report_template.md         # Template gốc của lab

docker-compose.yml           # Redis 7 Alpine
```

---

## Deliverables nộp bài

| # | File | Mô tả |
|---|---|---|
| 1 | `src/reliability_lab/` | Source code đã implement đầy đủ |
| 2 | `reports/metrics.json` | Metrics từ `make run-chaos` |
| 3 | `reports/metrics.csv` | CSV export |
| 4 | `reports/final_report.md` | Báo cáo đầy đủ sections |
| 5 | `docker-compose.yml` | Redis cho grader |
| 6 | Test log | `35 passed, 7 xpassed` (gồm Redis tests) |

### Lệnh grader chạy

```bash
pip install -e ".[dev]"
docker compose up -d
make test
make run-chaos
```

---

## Implementation highlights

### Circuit breaker (`circuit_breaker.py`)

- `allow_request()`: CLOSED/HALF_OPEN cho phép; OPEN chờ `reset_timeout_seconds` rồi chuyển HALF_OPEN.
- `record_failure()`: dùng `if/elif` — HALF_OPEN → `"probe_failure"`, CLOSED đạt threshold → `"failure_threshold_reached"`.
- `call()`: fail-fast khi OPEN, không retry storm.

### Semantic cache (`cache.py`)

- `similarity()`: cosine trên word tokens + character 3-grams.
- Privacy: regex chặn `password`, `balance`, `ssn`, ...
- False-hit: từ chối khi 4-digit numbers khác nhau (năm, ID).
- `SharedRedisCache`: HSET + EXPIRE, SCAN similarity lookup.

### Chaos & metrics (`chaos.py`)

- `ThreadPoolExecutor` với 4 workers cho concurrent load.
- `evaluate_scenario_pass()`: tiêu chí pass/fail riêng từng scenario.
- `run_cache_comparison()`: so sánh cache disabled vs Redis enabled.
- Export JSON mở rộng: `scenario_details`, `cache_comparison`, `concurrent_workers`.

---

## Rubric mapping

| Hạng mục | Điểm | Evidence trong project |
|---|---:|---|
| Circuit breaker & fallback | 25 | `circuit_breaker.py`, `gateway.py`, 12 CB tests pass |
| In-memory cache & cost | 15 | `ResponseCache`, false-hit log, cost_saved trong metrics |
| Redis shared cache | 15 | `SharedRedisCache`, 6 Redis tests pass, KEYS evidence |
| Observability & metrics | 15 | `metrics.json`, `metrics.csv`, P50/P95/P99 |
| Chaos & load testing | 15 | 3 scenarios pass, concurrent load, cache A/B |
| Report & code quality | 15 | `final_report.md` đầy đủ, type hints, tests pass |

---

## Tác giả

**Nguyễn Văn Chung** — Mã SV: **2A202600647**

Phase 2, Track 3, Day 10 — Reliability Engineering for Production Agents
