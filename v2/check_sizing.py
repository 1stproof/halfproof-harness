"""Per-arm Slurm sizing (operator ~17:20 PDT: one small job per arm).

Measured on rye-01 today (sacct): two-swarm checks used 1.2-1.3 CPU on average
(TotalCPU / Elapsed) and 7.3-12.2 GiB peak RSS for both arms; the four-solo
check used 0.7 CPU and 6.0 GiB for four arms. The partition allots about one
CPU per 3.8 GiB, so memory sets the CPU count.
"""
PER_ARM = {'swarm': dict(cpus=4, memory_gib=12), 'solo': dict(cpus=2, memory_gib=6),
           'open': dict(cpus=8, memory_gib=28)}  # open swarm: 5 members, then 4-5 closing sessions at once


# Mixed-swarm: 2026-10-13 measured 20G OOMs; 64G envelope in memleak-result.md.
# This is allocation sizing, not a cap on agent concurrency or research budget.
PER_ARM.update({'dsf-swarm': dict(cpus=8, memory_gib=24), 'mixed-swarm': dict(cpus=18, memory_gib=64)})


def _kind(arm):
    if arm in ('dsf-swarm', 'mixed-swarm'):
        return arm
    return 'open' if arm == 'open-swarm' else 'swarm' if arm.endswith('-swarm') else 'solo'


def size(arms):
    cpus = sum(PER_ARM[_kind(a)]['cpus'] for a in arms)
    mem = sum(PER_ARM[_kind(a)]['memory_gib'] for a in arms)
    return dict(cpus=max(18,cpus), memory_gib=max(64,mem))
