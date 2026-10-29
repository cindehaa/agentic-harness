# Progress

**Last updated:** 2026-10-28
**Now:** Phase 0: verifying both examples run, reading benchmark scripts and mini-swe-agent's DockerEnvironment.
**Next:** Phase 1: core types (`Task`, `GradeResult`), `Benchmark` ABC + registry, `Environment` ABC + `LocalEnvironment`.

Legend: 🟩 done · 🟨 in progress · 🟥 next up · ⬜ not started

```mermaid
flowchart TB
  subgraph P0["Phase 0: get oriented"]
    direction LR
    repo["Repo flattened to root"]
    base["Baseline commit + submodule"]
    notes["Design notes (ideation/)"]
    cmd["CLAUDE.md working rules"]
    ex["Run bash + python examples"]
    read["Read benchmark scripts + DockerEnvironment"]
  end

  subgraph P1["Phase 1: core types + benchmark adapters"]
    direction TB
    types["Task / GradeResult"]
    benchabc["Benchmark ABC"]
    reg["Benchmark registry"]
    envabc["Environment ABC"]
    localenv["LocalEnvironment"]
    bashad["BashOperations adapter"]
    pyad["PythonTasks adapter"]
    oracle["Oracle solutions + adapter tests"]
    tasks["harness tasks"]
  end

  subgraph P2["Phase 2: Docker environment"]
    direction TB
    dockenv["DockerEnvironment"]
    mount["Workspace mount + grade location"]
    cleanup["Guaranteed cleanup"]
  end

  subgraph P3["Phase 3: agent + trial runner"]
    direction TB
    agentabc["Agent ABC"]
    mini["MiniSweAgent adapter"]
    subgraph TR["Trial runner"]
      direction LR
      setup["setup"] --> agentph["agent"] --> grade["grade"] --> record["record"]
    end
    fb["Fallbacks: retry setup, timeout→grade, regrade"]
    runsync["harness run (sync)"]
  end

  subgraph P4["Phase 4: persistence + status/results"]
    direction TB
    subgraph DB["SQLite"]
      direction LR
      truns[("runs")] --> ttrials[("trials")] --> tatt[("attempts")]
      tatt --> tev[("events")]
      tatt --> tart[("artifacts")]
    end
    traj["Trajectory → events"]
    files["Artifacts on disk"]
    status["harness status"]
    results["harness results"]
  end

  subgraph P5["Phase 5: queue + workers"]
    direction TB
    enqueue["harness run enqueues"]
    claim["Atomic claim + lease"]
    worker["harness worker --concurrency N"]
    retry["Retry policy: failed vs error"]
  end

  subgraph P6["Phase 6: hardening"]
    direction LR
    prepull["Image pre-pull"]
    cost["Cost / token tracking"]
    breakit["Break-it tests"]
  end

  subgraph P7["Phase 7: extensions"]
    direction TB
    claude["ClaudeCode adapter"]
    validate["harness validate"]
    nullor["Null / Oracle agents"]
    skill["Add-benchmark skill"]
  end

  subgraph P8["Phase 8: deliverables"]
    direction LR
    readme["README"]
    diagram["Architecture diagram"]
    demo["Demo script"]
  end

  %% flow between pieces
  repo --> base --> notes --> cmd
  ex --> read
  P0 --> P1
  types --> benchabc --> reg
  benchabc --> bashad
  benchabc --> pyad
  envabc --> localenv
  bashad --> oracle
  pyad --> oracle
  reg --> tasks
  envabc --> dockenv
  dockenv --> mount --> cleanup
  agentabc --> mini
  mini --> agentph
  bashad --> setup
  pyad --> grade
  dockenv --> setup
  TR --> fb
  TR --> runsync
  record --> tatt
  mini --> traj --> tev
  record --> files --> tart
  ttrials --> status
  tatt --> results
  enqueue --> ttrials
  worker --> claim --> ttrials
  worker --> TR
  claim --> retry
  P5 --> P6
  agentabc --> claude
  nullor --> validate
  oracle --> nullor
  validate --> skill
  P7 --> P8

  classDef done fill:#d3f0d3,stroke:#1e5e1e,color:#1e5e1e
  classDef wip fill:#fff1b8,stroke:#6b5300,color:#6b5300
  classDef next fill:#f8d0d0,stroke:#7a1f1f,color:#7a1f1f
  classDef todo fill:#ececec,stroke:#999,color:#555

  class repo,base,notes,cmd done
  class ex,read wip
  class types,benchabc,reg,envabc,localenv next
  class bashad,pyad,oracle,tasks,dockenv,mount,cleanup,agentabc,mini,setup,agentph,grade,record,fb,runsync,truns,ttrials,tatt,tev,tart,traj,files,status,results,enqueue,claim,worker,retry,prepull,cost,breakit,claude,validate,nullor,skill,readme,diagram,demo todo
```
