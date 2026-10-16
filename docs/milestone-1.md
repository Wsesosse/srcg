# SRCG Milestone 1: cgroup v2 quota and process lifecycle

Milestone 1 is a foreground C++ supervisor. It creates one cgroup per job,
sets a fixed RAM/swap limit, moves the child into that cgroup before `exec`,
monitors kernel counters, classifies cgroup OOM exits, and removes only empty
cgroups. It does not implement borrowing, checkpoint/restart, Python IPC, or a
VRAM policy.

## Build and unit tests

```bash
cmake -S . -B build-srcg -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON
cmake --build build-srcg --parallel
ctest --test-dir build-srcg --output-on-failure
```

The integration test returns CTest's skip code unless
`SRCG_TEST_CGROUP_PARENT` names an empty, writable cgroup v2 path delegated to
the current user. On a correctly delegated test host:

```bash
SRCG_TEST_CGROUP_PARENT=/sys/fs/cgroup/srcg-integration \
ctest --test-dir build-srcg -R srcg_cgroup_integration_tests -V
```

The integration test verifies that a forked worker inherits its job cgroup and
that a deliberate memory-limit breach increments `memory.events:oom_kill`.
Do not point it at a cgroup containing unrelated processes.

## Delegation contract

SRCG never invokes `sudo`, changes ownership, or silently edits system service
configuration. An administrator or service manager must provide an empty
parent cgroup on a cgroup v2 mount, make it writable by the SRCG service user,
and make the `memory` controller available. The parent must not contain the
SRCG CLI process because cgroup v2 domain controllers cannot normally be
enabled below a non-root cgroup that contains processes.

Check without changing the system:

```bash
build-srcg/srcg check --parent /sys/fs/cgroup/srcg
```

`check` reports the filesystem version, memory-controller state, parent
existence, writability, and concrete setup problems.

## Initialize the parent and run a job

All values are converted to bytes internally. IEC (`GiB`, `MiB`) and decimal
(`GB`, `MB`) input units are distinct. Swap defaults to zero but is always
logged.

Initialize the parent once:

```bash
build-srcg/srcg init \
  --parent /sys/fs/cgroup/srcg \
  --parent-memory 6GiB \
  --parent-swap 0 \
  --host-headroom 2GiB
```

Then run jobs without rewriting parent settings:

```bash
build-srcg/srcg run \
  --parent /sys/fs/cgroup/srcg \
  --job training-A \
  --memory 2GiB \
  --swap 0 \
  --interval-ms 500 \
  --log srcg-logs/training-A.log \
  -- python3 train.py
```

The command remains in the foreground and supervises the training process.
The terminal's stdout and stderr belong to the training process; SRCG does not
mix lifecycle or monitoring records into them. `--log FILE` is optional. When
present, SRCG appends one-line `key=value` records to that text file containing
`memory.current`, `memory.peak`, host `MemAvailable`, free swap, PSI values
when supported, and OOM counters. With no `--log`, telemetry is not persisted.

Inspect or safely clean up a cgroup left after a supervisor crash:

```bash
build-srcg/srcg status --parent /sys/fs/cgroup/srcg --job training-A
build-srcg/srcg cleanup --parent /sys/fs/cgroup/srcg --job training-A
```

Cleanup fails while `cgroup.events` says the group is populated or while
`cgroup.procs` contains a PID. `memory.current` can remain nonzero after the
last process exits because of page-cache or kernel charges; it is logged but
is not used as a liveness test. The kernel reparents residual charges when the
empty cgroup is removed.

Reusing a job ID automatically removes an empty stale cgroup before creating
the new run. An existing populated cgroup is treated as an active job and is
never replaced. During a run, SRCG forwards `SIGINT`, `SIGTERM`, and `SIGHUP`
to the child, waits for it, and performs normal cleanup. `SIGKILL` cannot be
caught; a later run recovers the empty stale cgroup.
