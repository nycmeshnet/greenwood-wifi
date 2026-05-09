"""Integer Linear Programming optimizer for AP placement (PuLP + CBC)."""

import numpy as np
import pulp

# Marginal-solar APs are penalised in the objective so the solver prefers
# viable alternatives whenever they exist.
MARGINAL_PENALTY = 1.5


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
        (unviable candidates should be filtered out before calling this)

    Returns
    -------
    List of selected candidate indices.
    """
    n_candidates, n_test = coverage_matrix.shape

    prob = pulp.LpProblem("ap_placement", pulp.LpMinimize)

    x = [pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(n_candidates)]

    weights = [
        MARGINAL_PENALTY if solar_statuses[i] == "marginal" else 1.0
        for i in range(n_candidates)
    ]
    prob += pulp.lpSum(weights[i] * x[i] for i in range(n_candidates))

    # Every test point must be covered by at least one selected AP.
    # Points with no covering candidate are skipped (they appear in summary).
    for j in range(n_test):
        covering = [i for i in range(n_candidates) if coverage_matrix[i, j]]
        if covering:
            prob += pulp.lpSum(x[i] for i in covering) >= 1

    solver = pulp.PULP_CBC_CMD(msg=0)
    status = prob.solve(solver)

    selected = [
        i for i in range(n_candidates)
        if pulp.value(x[i]) is not None and pulp.value(x[i]) > 0.5
    ]

    print(
        f"ILP solved ({pulp.LpStatus[status]}): "
        f"{len(selected)} APs cover {_covered_count(coverage_matrix, selected)} "
        f"/ {n_test} test points"
    )
    return selected


def _covered_count(matrix: np.ndarray, selected: list[int]) -> int:
    if not selected:
        return 0
    return int(matrix[selected].any(axis=0).sum())
