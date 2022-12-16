from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from typing import Iterable

import cvxpy as cp
import numpy as np
from cvxpy.atoms.atom import Atom
from cvxpy.constraints import ExpCone
from cvxpy.constraints.constraint import Constraint

from dsp.cone_transforms import (
    K_repr_bilin,
    K_repr_FxGy,
    KRepresentation,
    LocalToGlob,
    affine_to_canon,
    switch_convex_concave,
)
from dsp.local import LocalVariable, LocalVariableError
from dsp.parser import DSPError, Parser, initialize_parser


class ConvexConcaveAtom(Atom, ABC):
    @abstractmethod
    def get_K_repr(self, local_to_glob: LocalToGlob, switched: bool = False) -> KRepresentation:
        pass

    @abstractmethod
    def get_convex_variables(self) -> list[cp.Variable]:
        pass

    @abstractmethod
    def get_concave_variables(self) -> list[cp.Variable]:
        pass

    @abstractmethod
    def get_convex_expression(self) -> cp.Expression:
        pass

    @abstractmethod
    def get_concave_expression(self) -> cp.Expression:
        pass

    def is_atom_convex(self) -> bool:
        return False

    def is_atom_concave(self) -> bool:
        return False

    def graph_implementation(
        self, arg_objs: list, shape: tuple[int, ...], data=None  # noqa
    ):  # noqa
        raise NotImplementedError

    def _grad(self, values: list):  # noqa
        raise NotImplementedError


class saddle_inner(ConvexConcaveAtom):
    def __init__(self, Fx: cp.Expression, Gy: cp.Expression) -> None:
        assert isinstance(Fx, cp.Expression)
        assert isinstance(Gy, cp.Expression)

        if not (Fx.is_affine() and Gy.is_affine()):
            assert Fx.is_convex(), "Fx must be convex"
            assert Fx.is_nonneg(), "Fx must be non-negative"
            assert Gy.is_concave(), "Gy must be concave"

            if not Gy.is_nonneg():
                warnings.warn(
                    "Gy is non-positive. The y domain of convex_concave_inner is Gy >="
                    " 0. The implicit constraint Gy >= 0 will be added to the problem."
                )
            self.bilinear = False
        else:
            self.bilinear = True

        self.Fx = Fx
        self.Gy = Gy

        assert len(Fx.shape) <= 1 or (
            len(Fx.shape) == 2 and min(Fx.shape) == 1
        )  # TODO: implement matrix inputs
        assert Fx.shape == Gy.shape or Fx.size == Gy.size

        super().__init__(Fx, Gy)

    def get_concave_expression(self) -> cp.Expression:
        Fx = self.Fx
        assert Fx.value is not None
        Fx = np.reshape(Fx.value, (Fx.size,), order="F")

        Gy = self.Gy
        Gy = cp.reshape(Gy, (Gy.size,), order="F")

        return Fx @ Gy

    def get_convex_expression(self) -> cp.Expression:
        Gy = self.Gy
        assert Gy.value is not None
        Gy = np.reshape(Gy.value, (Gy.size,), order="F")

        Fx = self.Fx
        Fx = cp.reshape(Fx, (Fx.size,), order="F")

        return Fx @ Gy

    def get_K_repr(self, local_to_glob: LocalToGlob, switched: bool = False) -> KRepresentation:
        if self.bilinear:
            K_out = (
                K_repr_bilin(self.Fx, self.Gy, local_to_glob)
                if not switched
                else K_repr_bilin(-self.Gy, self.Fx, local_to_glob)
            )
        else:
            K_out = K_repr_FxGy(self.Fx, self.Gy, local_to_glob, switched)
            if not self.Gy.is_nonneg():
                outer_constraints = K_out.y_constraints if not switched else K_out.constraints
                outer_constraints.append(self.Gy >= 0)

        if not switched:
            K_out.concave_expr = lambda x: self.get_concave_expression()
        else:
            K_out.concave_expr = lambda x: -self.get_convex_expression()
        return K_out

    def get_convex_variables(self) -> list[cp.Variable]:
        return self.Fx.variables()

    def get_concave_variables(self) -> list[cp.Variable]:
        return self.Gy.variables()

    def shape_from_args(self) -> tuple[int, ...]:
        return ()

    def sign_from_args(self) -> tuple[bool, bool]:
        return (True, False)

    def is_incr(self, idx: int) -> bool:
        return False  # increasing in both arguments since y nonneg

    def is_decr(self, idx: int) -> bool:
        return False


class inner(saddle_inner):
    def __init__(self, Fx: cp.Expression, Gy: cp.Expression) -> None:
        assert isinstance(Fx, cp.Expression)
        assert isinstance(Gy, cp.Expression)

        assert Fx.is_affine() and Gy.is_affine(), (
            "inner must be affine. Use convex-concave-inner for non-affine" " bilinear terms."
        )

        super().__init__(Fx, Gy)

    def get_concave_expression(self) -> cp.Expression:
        return self.Fx.value @ self.Gy

    def get_convex_expression(self) -> cp.Expression:
        return self.Fx @ self.Gy.value


class weighted_log_sum_exp(ConvexConcaveAtom):
    def __init__(self, exponents: cp.Expression, weights: cp.Expression) -> None:
        """
        Implements the function f(x,y) = log(sum(exp(x_i) * y_i)) for vectors x and y.
        The weights, y, must be non-negative. If they are non recognized by
        cvxpy as nonnegative, an implicit domain constraint is added and a
        warning is provided.
        The exponents can be any convex cvxpy expression.

        The conic representation is:

            log sum_i y_i exp(x_i)
            min. f, t, u    f @ y + t
            subject to      f >= exp(x+u)
                            t >= -u-1

        """

        assert isinstance(exponents, cp.Expression)
        assert isinstance(weights, cp.Expression)
        assert exponents.is_convex(), "exponents must be convex"

        # assert weights.is_affine(), "weights must be affine"
        assert weights.is_concave(), "weights must be concave"

        if not weights.is_nonneg():
            warnings.warn(
                "Weights are non-positive. The domain of weighted log-sum-exp is y >="
                " 0. The implicit constraint y >= 0 will be added to"
                " the problem."
            )

        self.concave_composition = not weights.is_affine()

        self.exponents = exponents
        self.weights = weights

        assert len(exponents.shape) <= 1 or (
            len(exponents.shape) == 2 and min(exponents.shape) == 1
        )  # TODO: implement matrix inputs
        assert exponents.shape == weights.shape or exponents.size == weights.size

        super().__init__(exponents, weights)

    def get_concave_expression(self, eps: float = 1e-6) -> cp.Expression:
        x = self.exponents

        # assert x.value is not None, f"{x.name()} must have a value"
        if x.value is None:
            return None

        x = np.reshape(x.value, (x.size,), order="F")

        y = self.weights
        y = cp.reshape(y, (y.size,), order="F")

        arg = y @ np.exp(x)
        return cp.log(arg)

    def get_convex_expression(self, eps: float = 1e-6) -> cp.Expression:
        x = self.weights
        # assert x.value is not None
        if x.value is None:
            return None

        x = np.reshape(x.value, (x.size,), order="F")

        y = self.exponents
        y = cp.reshape(y, (y.size,), order="F")

        nonneg = np.where(x > eps)[0]
        arg = y[nonneg] + np.log(x)[nonneg]
        return cp.log_sum_exp(arg)

    def get_K_repr(self, local_to_glob: LocalToGlob, switched: bool = False) -> KRepresentation:
        z = cp.Variable(self.weights.size, name="z_wlse") if self.concave_composition else None
        f_local = cp.Variable(self.weights.size, name="f_wlse")
        t = cp.Variable(name="t_wlse")
        u = cp.Variable(name="u_wlse")

        epi_exp = cp.Variable(self.exponents.size, name="exp_epi")
        constraints = [
            epi_exp >= self.exponents,  # handles composition in exponent
            ExpCone(cp.reshape(epi_exp + u, (f_local.size,)), np.ones(f_local.size), f_local),
            t >= -u - 1,
        ]

        t_global = cp.Variable(name="t_global")
        if self.concave_composition:
            constraints += [t_global == t]
            f_global = cp.Variable(self.weights.size, name="f_global_wlse_comp")
            constraints += [f_global == f_local]
        else:
            B, c = affine_to_canon(self.weights, local_to_glob, switched)
            constraints += [t_global == t + f_local @ c]
            f_global = cp.Variable(
                local_to_glob.y_size if not switched else local_to_glob.x_size,
                name="f_global_wlse",
            )
            constraints += [f_global == B.T @ f_local]

        K_repr = KRepresentation(
            f=f_global,
            t=t_global,
            constraints=constraints,
        )

        switching_variables = self.exponents.variables()  # if not self.concave_composition else [z]
        precomp = z if self.concave_composition else None
        K_out = (
            switch_convex_concave(
                constraints,
                f_global,
                t_global,
                switching_variables,
                local_to_glob,
                precomp,
            )
            if switched
            else K_repr
        )

        if self.concave_composition:
            if not switched:
                x_vars_1 = (
                    self.get_convex_variables() if not switched else self.get_concave_variables()
                )
                K_switch_1 = switch_convex_concave(
                    K_out.constraints,
                    K_out.f,
                    K_out.t,
                    x_vars_1,
                    local_to_glob,
                    precomp=z,
                )
                K_switch_1.constraints += [self.weights >= z]

                # dualize the outer concave exp variables if switched
                x_vars_2 = (
                    self.get_concave_variables() if not switched else self.get_convex_variables()
                )
                K_out = switch_convex_concave(
                    K_switch_1.constraints,
                    K_switch_1.f,
                    K_switch_1.t,
                    x_vars_2,
                    local_to_glob,
                )

            else:
                K_out.constraints += [z <= self.weights]

        if not self.weights.is_nonneg():
            if switched:
                K_out.constraints += [self.weights >= 0]
            else:
                K_out.y_constraints += [self.weights >= 0]

        concave_fun = (
            (lambda x: self.get_concave_expression())
            if not switched
            else (lambda x: -self.get_convex_expression())
        )
        K_out.concave_expr = concave_fun

        return K_out

    def get_convex_variables(self) -> list[cp.Variable]:
        return self.exponents.variables()

    def get_concave_variables(self) -> list[cp.Variable]:
        return self.weights.variables()

    def shape_from_args(self) -> tuple[int, ...]:
        return ()

    def sign_from_args(self) -> tuple[bool, bool]:
        return (False, False)

    def is_incr(self, idx: int) -> bool:
        return True  # increasing in both arguments since y nonneg


def init_parser_wrapper(
    expr: cp.Expression,
    constraints: list[Constraint],
    variables: set[cp.Variable],
    mode: str,
    other_variables: list[cp.Variable] | None = None,
) -> Parser:
    assert mode in ["sup", "inf"]

    expr = expr if mode == "sup" else -expr

    other_variables = other_variables if other_variables is not None else []

    parser = initialize_parser(
        expr,
        minimization_vars=other_variables,
        maximization_vars=variables,
        constraints=constraints,
    )

    return parser


class SaddleExtremum(Atom):
    def _validate_arguments(
        self, constraints: list[Constraint], variables: list[LocalVariable]
    ) -> None:
        assert self.f.size == 1
        assert isinstance(variables, Iterable)
        if any([not isinstance(v, LocalVariable) for v in variables]):
            raise LocalVariableError("All provided variables must be instances of LocalVariable.")
        assert isinstance(constraints, Iterable)
        for c in constraints:
            for v in c.variables():
                if not isinstance(v, LocalVariable):
                    raise LocalVariableError(
                        "All variables in constraints must be instances of" "LocalVariable."
                    )
        assert isinstance(self.f, cp.Expression)

    def is_dsp(self) -> bool:
        try:
            self.parser
            return True
        except DSPError:
            return False


class saddle_max(SaddleExtremum):
    r"""sup_{y\in Y}f(x,y)"""

    def __init__(
        self,
        f: cp.Expression,
        concave_vars: Iterable[LocalVariable],
        constraints: Iterable[Constraint],
    ) -> None:
        self.f = f

        self._validate_arguments(constraints, concave_vars)
        self.constraints = list(constraints)
        self.concave_vars = list(concave_vars)  # variables to maximize over

        self._parser = None
        self.other_variables = [
            v for v in f.variables()
            if (v not in set(concave_vars) and not isinstance(v, LocalVariable))
        ]
        self.is_dsp()  # Make sure local variable expressions are set
        super().__init__(*self.other_variables)

    @property
    def convex_vars(self) -> list[cp.Variable]:
        return self.parser.convex_vars

    @property
    def parser(self) -> Parser:
        if self._parser is None:
            parser = init_parser_wrapper(
                self.f,
                self.constraints,
                set(self.concave_vars),
                mode="sup",
                other_variables=self.other_variables,
            )

            all_concave_vars_specified = set(self.concave_vars) == set(parser.concave_vars)
            all_concave_vars_local = all([isinstance(v, LocalVariable) for v in self.concave_vars])

            if not all_concave_vars_local:
                raise LocalVariableError(
                    "All concave variables must be instances of" "LocalVariable."
                )

            if not all_concave_vars_specified:
                raise DSPError(
                    "Must specify all concave variables, which all must be instances of"
                    "LocalVariable."
                )

            for v in self.concave_vars:
                v.expr = self

            self._parser = parser
            return parser
        else:
            return self._parser

    def name(self) -> str:
        return (
            "saddle_max("
            + self.f.name()
            + ", ["
            + "".join([str(c) for c in self.constraints])
            + "])"
        )

    def numeric(self, values: list) -> np.ndarray | None:
        r"""
        Compute sup_{y\in Y}f(x,y) numerically
        """

        local_to_glob_y = LocalToGlob(self.convex_vars, self.concave_vars)

        K_repr = self.parser.parse_expr_repr(self.f, switched=False, local_to_glob=local_to_glob_y)

        ccv = K_repr.concave_expr(values)
        if ccv is None:
            return None
        else:
            aux_problem = cp.Problem(cp.Maximize(ccv), self.constraints)
            aux_problem.solve()
            return aux_problem.value

    def sign_from_args(self) -> tuple[bool, bool]:
        is_positive = self.f.is_nonneg()
        is_negative = self.f.is_nonpos()

        return is_positive, is_negative

    def is_atom_convex(self) -> bool:
        """Is the atom convex?"""
        return self.is_dsp()

    def is_atom_concave(self) -> bool:
        """Is the atom concave?"""
        return False

    def is_incr(self, idx: int) -> bool:
        return False

    def is_decr(self, idx: int) -> bool:
        return False

    def shape_from_args(self) -> tuple[int, ...]:
        """Returns the (row, col) shape of the expression."""
        return self.f.shape


class saddle_min(SaddleExtremum):
    r"""inf_{x\in X}f(x,y)"""

    def __init__(
        self,
        f: cp.Expression,
        convex_vars: Iterable[LocalVariable],
        constraints: Iterable[Constraint],
    ) -> None:
        self.f = f

        self._validate_arguments(constraints, convex_vars)
        self.constraints = list(constraints)
        self.convex_vars = list(convex_vars)  # variables to minimize over

        self._parser = None
        assert self._parser is None

        # self.other_variables = [
        #     v for v in f.variables()
        #     if (v not in set(convex_vars) and not isinstance(v, LocalVariable))
        # ]
        other_variables = []
        for v in f.variables():
            if (v not in set(convex_vars) and not isinstance(v, LocalVariable)):
                other_variables.append(v)
        self.other_variables = other_variables

        self.is_dsp()  # Make sure local variable expressions are set
        super().__init__(*other_variables)

    @property
    def concave_vars(self) -> list[cp.Variable]:
        return self.parser.concave_vars

    @property
    def parser(self) -> Parser:
        if self._parser is None:
            print(f"{self.other_variables=}")
            parser = init_parser_wrapper(
                self.f,
                self.constraints,
                set(self.convex_vars),
                mode="inf",
                other_variables=self.other_variables,
            )

            all_convex_vars_specified = set(self.convex_vars) == set(parser.concave_vars)
            all_convex_vars_local = all([isinstance(v, LocalVariable) for v in self.convex_vars])

            if not all_convex_vars_local:
                raise LocalVariableError(
                    "All convex variables must be instances of" "LocalVariable."
                )

            if not all_convex_vars_specified:
                raise DSPError(
                    "Must specify all convex variables, which all must be instances of"
                    "LocalVariable."
                )

            for v in self.convex_vars:
                v.expr = self

            self._parser = parser
            return parser
        else:
            return self._parser

    def name(self) -> str:
        return (
            "saddle_min("
            + self.f.name()
            + ", ["
            + "".join([str(c) for c in self.constraints])
            + "])"
        )

    def numeric(self, values: list) -> np.ndarray | None:
        r"""
        Compute inf_{x\in X}f(x,y) numerically via -sup_{x\in X}-f(x,y)
        """
        neg_parser = init_parser_wrapper(
            -self.f, self.constraints, set(self.convex_vars), mode="sup"
        )
        neg_local_to_glob = LocalToGlob(neg_parser.convex_vars, neg_parser.concave_vars)
        neg_K_repr = neg_parser.parse_expr_repr(
            -self.f, switched=False, local_to_glob=neg_local_to_glob
        )

        ccv = neg_K_repr.concave_expr(values)
        if ccv is None:
            return None
        else:
            aux_problem = cp.Problem(cp.Minimize(-ccv), self.constraints)
            aux_problem.solve()
            return aux_problem.value

    def sign_from_args(self) -> tuple[bool, bool]:
        is_positive = self.f.is_nonneg()
        is_negative = self.f.is_nonpos()

        return is_positive, is_negative

    def is_atom_convex(self) -> bool:
        """Is the atom convex?"""
        return False

    def is_atom_concave(self) -> bool:
        """Is the atom concave?"""
        return self.is_dsp()

    def is_incr(self, idx: int) -> bool:
        return False

    def is_decr(self, idx: int) -> bool:
        return False

    def shape_from_args(self) -> tuple[int, ...]:
        """Returns the (row, col) shape of the expression."""
        return self.f.shape


class saddle_quad_form(ConvexConcaveAtom):
    def __init__(self, x: cp.Expression, P: cp.Expression) -> None:
        assert isinstance(x, cp.Expression)
        assert isinstance(P, cp.Expression)
        assert P.is_psd()

        assert x.size == P.shape[0]

        self.x = x
        self.P = P

        super().__init__(x, P)

    def get_concave_expression(self) -> cp.Expression:
        x = self.x
        assert x.value is not None
        x = np.reshape(x.value, (x.size,), order="F")

        P = self.P

        return x.T @ P @ x

    def get_convex_expression(self) -> cp.Expression:
        P = self.P
        assert P.value is not None
        P = np.reshape(P.value, (P.shape,), order="F")

        x = self.x

        return cp.quad_form(x, P)

    def get_K_repr(self, local_to_glob: LocalToGlob, switched: bool = False) -> KRepresentation:
        n = self.x.size
        F_local = cp.Variable((n, n), name="F_quad_form_local", PSD=True)
        A = cp.Variable((n + 1, n + 1), name="A_quad_form", PSD=True)
        t = cp.Variable(name="t_quad_form")

        constraints = [
            A[:n, :n] == F_local,
            A[-1, -1] == 1,
            A[:n, -1] == self.x,
            A[-1, :n] == self.x.T,
            t == 0,
        ]

        B, c = affine_to_canon(self.P, local_to_glob, switched)
        F_global = cp.Variable(
            local_to_glob.y_size if not switched else local_to_glob.x_size,
            name="f_global_saddle_quad_form",
        )
        constraints += [F_global == B.T @ cp.vec(F_local)]

        K_repr = KRepresentation(
            f=F_global,
            t=t,
            constraints=constraints,
            concave_expr=lambda x: self.get_concave_expression(),
        )

        if switched:
            K_repr = switch_convex_concave(
                constraints, F_global, t, self.get_convex_variables(), local_to_glob
            )
            K_repr.concave_expr = lambda x: self.get_convex_expression()

        return K_repr

    def name(self) -> str:
        return "saddle_quad_form(" + self.x.name() + ", " + self.P.name() + ")"

    def get_convex_variables(self) -> list[cp.Variable]:
        return self.x.variables()

    def get_concave_variables(self) -> list[cp.Variable]:
        return self.P.variables()

    def shape_from_args(self) -> tuple[int, ...]:
        return ()

    def sign_from_args(self) -> tuple[bool, bool]:
        return (True, False)

    def is_incr(self, idx: int) -> bool:
        return False

    def is_decr(self, idx: int) -> bool:
        return False
