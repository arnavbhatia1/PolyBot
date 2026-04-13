 Testing & Validation Infrastructure
What it covers:

602 tests — what's actually covered vs what's assumed tested?
Paper mode as integration test — fidelity gaps vs live
Counterfactual tracking accuracy (do what-ifs match reality?)
Backtest correctness in WeightOptimizer (lookahead bias risk)
Edge case coverage: empty book, zero ATR, Chainlink unavailable
Regression tests for the sizing chain (12 multipliers = 12 failure points)
Signal engine unit tests (are all 10 layers independently tested?)
Mock WebSocket feeds for CI (or are tests using live data?)
Performance benchmarks (does 1-2ms per tick hold under load?)
The --run-pipeline mode as a standalone test target

Why it's #17: 602 tests sounds like a lot until one untested edge case causes a live trade at $0 fill price. SIMPLIFY AND MAKE RELAISTIC AS MUCH AS POSSIBLE