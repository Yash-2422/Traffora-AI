# Traffora-AI Central Orchestrator — Execution Plan

## 1. Inspect (done)
- [x] Clone integration branch and map repository
- [x] Read all three agent READMEs
- [x] Inspect agent schemas and public functions
- [x] Inspect SUMO connection implementations and networks
- [x] Identify integration constraints (3 networks, ParkSense global TraCI, GC direct signal writes)

## 2. Shared SUMO scenario
- [x] Build one unified scenario on GreenCorridor 5x5 network (TLS + ambulance)
- [x] Add parking areas, zones, parking demand routes, hospital POI
- [x] Validate unified scenario runs in SUMO

## 3. Orchestrator package
- [x] exceptions.py, config.py, schemas.py
- [x] shared_state.py, agent_registry.py (adapters)
- [x] event_manager.py
- [x] safety_validator.py
- [x] decision_engine.py
- [x] simulation_coordinator.py
- [x] orchestrator.py
- [x] scripts/run_orchestrator.py

## 4. Tests
- [x] Unit tests for each module (11 + 13 + 16 + 10 + 13 + 16 + 15 = 94)
- [x] Integration test with mocked SUMO (4)
- [x] End-to-end test with real SUMO unified scenario (7)
- [x] Run existing agent suites (regression: TrafficMind 253, ParkSense 110, GreenCorridor 138)

## 5. Demonstration + metrics
- [x] Demo scenario with actual outputs (scripts/run_demo.py -> runs/demo.json)
- [x] Metrics collection and comparison harness (scripts/run_comparison.sh -> runs/cmp_*.json)

## 6. Documentation
- [x] backend/orchestrator/README.md with architecture, integration, PowerShell commands

## 7. Git
- [x] Logical commits on integration branch only (10 commits local; push awaiting user authorisation)

## 8. HTTP API layer
- [x] api.py: FastAPI over the existing service methods (transport only)
- [x] 17 transport tests with fakes
- [x] 8 end-to-end tests against real SUMO
- [x] requirements-orchestrator.txt (README referenced a non-existent requirements.txt)
- [x] Live server verified over HTTP: 3 agents READY, real P1 preemption at C1

## 9. Deployment readiness
- [x] Fix api.py entry point: honour $PORT, bind 0.0.0.0 in containers
- [x] Verify $PORT path locally (binds 0.0.0.0:8123, /health -> 200)
- [x] requirements-orchestrator-linux.txt with eclipse-sumo wheel (pip-resolvable)
- [x] Dockerfile + .dockerignore (build fails early if SUMO binary missing)
- [x] render.yaml blueprint (docker runtime, /health check, integration branch)
- [x] DEPLOYMENT.md: why Vercel cannot host it, how Render does
- [x] README deployment subsection pointing at DEPLOYMENT.md
- [x] Full suite still green after the entry-point change (131 passed)
