"""Set-cover ILP for AP placement using scipy.optimize.milp (HiGHS solver).

HiGHS accepts the coverage matrix as a scipy sparse matrix directly, avoiding
the per-constraint Python loop that made the old PuLP/CBC approach slow.
"""

import time

import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
from scipy.sparse import csc_matrix

from constants import MARGINAL_PENALTY


def optimize_placement(
    coverage_matrix: np.ndarray,
    solar_statuses: list[str],
) -> list[int]:
    """
    Minimise the weighted number of APs subject to covering every test point.

    Parameters
    ----------
    coverage_matrix : bool[n_candidates, n_test_points]
        True where candidate i covers test point j.
    solar_statuses  : list of 'viable' | 'marginal' per candidate
        (unviable candidates should already be filtered out)

    Returns
    -------
    List of selected candidate indices.
    """
    n_candidates, n_test = coverage_matrix.shape
    print(f"  Variables : {n_candidates} candidates")
    print(f"  Rows      : {n_test} test points")

    t0 = time.time()

    c = np.array(
        [MARGINAL_PENALTY if s == "marginal" else 1.0 for s in solar_statuses],
        dtype=np.float64,
    )

    # Drop test points that no candidate covers (they'll appear in the summary).
    has_coverage = coverage_matrix.any(axis=0)
    n_uncovered = int((~has_coverage).sum())
    if n_uncovered:
        print(f"  Warning   : {n_uncovered} test points have no covering candidate")

    # Constraint matrix A: each test point must be covered by ≥ 1 AP.
    # Shape (n_constrained, n_candidates) stored column-sparse for HiGHS.
    A = csc_matrix(coverage_matrix[:, has_coverage].T.astype(np.float64))
    n_constrained = A.shape[0]
    nnz = A.nnz
    print(f"  Constraints: {n_constrained}  non-zeros: {nnz:,}  "
          f"({time.time()-t0:.1f}s to build)")

    t1 = time.time()
    print("  Solving with HiGHS...", flush=True)

    result = milp(
        c,
        constraints=LinearConstraint(A, lb=1.0, ub=np.inf),
        integrality=np.ones(n_candidates, dtype=np.int8),
        bounds=Bounds(lb=0, ub=1),
    )

    elapsed = time.time() - t1

    if result.success:
        selected = [i for i, v in enumerate(result.x) if v > 0.5]
        covered = _covered_count(coverage_matrix, selected)
        print(
            f"  Optimal   : {len(selected)} APs cover {covered}/{n_test} "
            f"test points  ({elapsed:.1f}s)"
        )
        return selected

    print(f"  Solver status: {result.message}  ({elapsed:.1f}s)")
    print("  Falling back to greedy set-cover...")
    return _greedy_fallback(coverage_matrix, c)


def _greedy_fallback(coverage_matrix: np.ndarray, weights: np.ndarray) -> list[int]:
    """Greedy weighted set-cover — fast but not optimal."""
    uncovered = np.ones(coverage_matrix.shape[1], dtype=bool)
    selected = []
    remaining = set(range(coverage_matrix.shape[0]))

    while uncovered.any() and remaining:
        best_i = min(
            remaining,
            key=lambda i: weights[i] / max(coverage_matrix[i][uncovered].sum(), 1),
        )
        selected.append(best_i)
        remaining.discard(best_i)
        uncovered &= ~coverage_matrix[best_i]

    return selected


def _covered_count(matrix: np.ndarray, selected: list[int]) -> int:
    if not selected:
        return 0
    return int(matrix[selected].any(axis=0).sum())
