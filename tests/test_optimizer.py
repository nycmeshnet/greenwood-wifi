"""Tests for the ILP placement optimizer.

Uses synthetic coverage matrices so no external data is needed.
These tests guard the correctness of the set-cover logic and the marginal
penalty weighting.
"""

import numpy as np
import pytest
from optimize.placer import optimize_placement


class TestOptimizePlacement:
    def test_single_candidate_covers_all(self):
        # 1 candidate covers 3 test points — must be selected
        cov = np.array([[True, True, True]])
        selected = optimize_placement(cov, ["viable"])
        assert selected == [0]

    def test_two_candidates_one_sufficient(self):
        # Candidate 0 covers everything; candidate 1 covers nothing useful
        cov = np.array([
            [True, True, True],
            [False, False, False],
        ])
        selected = optimize_placement(cov, ["viable", "viable"])
        assert 0 in selected
        assert len(selected) == 1

    def test_two_candidates_both_needed(self):
        # Each candidate covers different points — both required
        cov = np.array([
            [True, True, False, False],
            [False, False, True, True],
        ])
        selected = optimize_placement(cov, ["viable", "viable"])
        assert set(selected) == {0, 1}

    def test_marginal_penalty_avoids_marginal_when_possible(self):
        # Candidate 0 (viable) covers point 0.
        # Candidate 1 (marginal) also covers point 0.
        # Optimizer should prefer candidate 0.
        cov = np.array([
            [True],
            [True],
        ])
        selected = optimize_placement(cov, ["viable", "marginal"])
        assert selected == [0], "Should prefer viable over marginal when both cover same point"

    def test_marginal_selected_when_only_option(self):
        # Only candidate 1 (marginal) covers point 1
        cov = np.array([
            [True, False],
            [False, True],
        ])
        selected = optimize_placement(cov, ["viable", "marginal"])
        assert 0 in selected
        assert 1 in selected

    def test_uncoverable_point_does_not_crash(self):
        # Point 1 has no covering candidate — should be skipped, not raise
        cov = np.array([
            [True, False],
        ])
        selected = optimize_placement(cov, ["viable"])
        assert selected == [0]

    def test_returns_list_of_ints(self):
        cov = np.array([[True, True]])
        result = optimize_placement(cov, ["viable"])
        assert isinstance(result, list)
        assert all(isinstance(i, int) for i in result)

    def test_minimum_set_cover(self):
        # 4 candidates, each covers 2 of 4 points — only 2 non-overlapping needed
        cov = np.array([
            [True,  True,  False, False],
            [True,  True,  False, False],  # duplicate of 0
            [False, False, True,  True],
            [False, False, True,  True],   # duplicate of 2
        ])
        selected = optimize_placement(cov, ["viable"] * 4)
        assert len(selected) == 2
