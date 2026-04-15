# Web3v2

Clean, layered project layout for the ACE simulation stack.

## Structure

```text
web3v2/
├─ src/
│  └─ ace_sim/
│     ├─ engine/                    # core ledger and market engine
│     ├─ execution/
│     │  ├─ action_registry/        # action schemas and validation
│     │  ├─ guardrails/             # auditor and safety checks
│     │  └─ orchestrator/           # tick/event scheduling
│     ├─ social/                    # topology, channels, perception filter
│     ├─ agents/                    # base agent and role variants
│     └─ __init__.py                # public exports
├─ tests/                           # automated tests
├─ scripts/
│  ├─ visualization/                # plotting and simulation traces
│  └─ reports/                      # readable reports
├─ docs/
│  └─ design/                       # design docs
├─ data/
│  └─ sqlite/                       # sample databases
├─ artifacts/                       # generated outputs
├─ pyproject.toml
└─ README.md
```

## Common commands

```powershell
pytest -q
```

```powershell
python scripts/visualization/ace_conservation_visualizer.py --agents 100 --rounds 1000 --sample-interval 10 --output-dir artifacts/conservation
```

```powershell
python scripts/visualization/phase2_orchestrator_visualizer.py --ticks 80 --num-retail 20 --seed 17 --output-dir artifacts/phase2
```

```powershell
python scripts/visualization/phase3_topology_visualizer.py --ticks 30 --communities 3 --retail-per-community 6 --seed 31 --output-dir artifacts/phase3
```

```powershell
python scripts/reports/ace_human_report.py --db data/sqlite/ace_demo.sqlite3
```
