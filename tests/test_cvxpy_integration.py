import cvxpy as cp
import numpy as np
import pytest

from dspp.atoms import concave_max, convex_min, inner, weighted_log_sum_exp
from dspp.cvxpy_integration import extend_cone_canon_methods
from dspp.problem import MinimizeMaximize, SaddleProblem

extend_cone_canon_methods()


def test_semi_infinite_matrix():
    x = cp.Variable(2, name="x", nonneg=True)
    y = cp.Variable(2, name="y", nonneg=True)

    A = np.array([[1, 2], [3, 4]])
    inner_expr = inner(x, A @ y)

    # Saddle problem
    saddle_obj = MinimizeMaximize(inner_expr)
    saddle_problem = SaddleProblem(saddle_obj, [cp.sum(x) == 1, cp.sum(y) == 1])
    saddle_problem.solve()

    assert np.isclose(saddle_problem.value, 2.0, atol=1e-4)
    assert np.allclose(x.value, [1, 0], atol=1e-4)
    assert np.allclose(y.value, [0, 1], atol=1e-4)

    # Concave max problem

    obj = cp.Minimize(concave_max(inner_expr, [y], [cp.sum(y) == 1]))
    constraints = [cp.sum(x) == 1]

    problem = cp.Problem(obj, constraints)
    problem.solve(solver=cp.SCS)

    obj = cp.Maximize(convex_min(inner_expr, [x], [cp.sum(x) == 1]))
    constraints = [cp.sum(y) == 1]

    problem = cp.Problem(obj, constraints)
    problem.solve(solver=cp.SCS)
    assert np.isclose(problem.value, 2.0, atol=1e-4)
    assert np.allclose(x.value, [1, 0], atol=1e-4)
    assert np.allclose(y.value, [0, 1], atol=1e-4)


def test_dcp_concave_max():
    x = cp.Variable(2, name="x", nonneg=True)
    y = cp.Variable(2, name="y", nonneg=True)

    A = np.array([[1, 2], [3, 4]])
    inner_expr = inner(x, A @ y)

    obj = cp.Maximize(concave_max(inner_expr, [y], [cp.sum(y) == 1]))
    assert not obj.is_dcp()


def test_semi_infinite_expr():
    x = cp.Variable(2, name="x", nonneg=True)
    y = cp.Variable(2, name="y", nonneg=True)

    wlse = weighted_log_sum_exp(x, y)

    sup_y_f = concave_max(2 * wlse + y[1] + cp.exp(x[1]), [y], [y <= 1])

    with pytest.raises(AssertionError, match="x must have a value"):
        sup_y_f.numeric(values=np.ones(1))

    x.value = np.ones(2)
    val = sup_y_f.numeric(values=np.ones(1))
    assert np.isclose(val, 2 * np.log(2 * np.e) + 1 + np.e)
