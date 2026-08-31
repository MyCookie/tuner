"""`python -m tuner` -- lets the driver invoke stage subprocesses via `sys.executable`
without depending on the `tuner` console script being resolvable on `PATH`
(01-architecture.md §2; PR #13 review round 1 nit)."""

from tuner.cli import main

if __name__ == "__main__":
    main()
