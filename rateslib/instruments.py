# -*- coding: utf-8 -*-

# Sphinx substitutions
"""
.. ipython:: python
   :suppress:

   from rateslib.instruments import *
   from rateslib.curves import Curve
   from datetime import datetime as dt
   from pandas import Series, date_range
   curve = Curve(
       nodes={
           dt(2022,1,1): 1.0,
           dt(2023,1,1): 0.99,
           dt(2024,1,1): 0.965,
           dt(2025,1,1): 0.93,
       },
       interpolation="log_linear",
   )
"""

from abc import abstractmethod, ABCMeta
from datetime import datetime
from typing import Optional, Union
import abc
import warnings

import numpy as np
from scipy.optimize import brentq
from pandas.tseries.offsets import CustomBusinessDay
from pandas import DataFrame, concat, date_range, Series

from rateslib import defaults
from rateslib.calendars import add_tenor, _add_days, get_calendar, dcf
from rateslib.scheduling import Schedule
from rateslib.curves import Curve, index_left, LineCurve
from rateslib.solver import Solver
from rateslib.periods import Cashflow, FixedPeriod, FloatPeriod, _get_fx_and_base
from rateslib.legs import (
    FixedLeg,
    FixedLegExchange,
    FloatLeg,
    FloatLegExchange,
    FloatLegExchangeMtm,
    FixedLegExchangeMtm,
    CustomLeg,
)
from rateslib.dual import Dual, Dual2, set_order
from rateslib.fx import FXForwards, FXRates


# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.


def _get_curve_from_solver(curve, solver):
    if getattr(curve, "_is_proxy", False):
        # proxy curves exist outside of solvers but still have Dual variables associated
        # with curves inside the solver, so can still generate risks to calibrating
        # instruments
        return curve
    else:
        if isinstance(curve, str):
            return solver.pre_curves[curve]
        else:
            try:
                # it is a safeguard to load curves from solvers when a solver is
                # provided and multiple curves might have the same id
                return solver.pre_curves[curve.id]
            except KeyError:
                if defaults.curve_not_in_solver == "ignore":
                    return curve
                elif defaults.curve_not_in_solver == "warn":
                    warnings.warn("`curve` not found in `solver`.", UserWarning)
                    return curve
                else:
                    raise ValueError("`curve` must be in `solver`.")


# def _get_curves_and_fx_maybe_from_solver(
#     solver: Optional[Solver],
#     curves: Union[Curve, str, list],
#     fx: Optional[Union[float, FXRates, FXForwards]],
# ):
#     """
#     Parses the ``solver``, ``curves`` and ``fx`` arguments in combination.
#
#     Returns
#     -------
#     tuple : (leg1 forecasting, leg1 discounting, leg2 forecasting, leg2 discounting), fx
#
#     Notes
#     -----
#     If only one curve is given this is used as all four curves.
#
#     If two curves are given the forecasting curve is used as the forecasting
#     curve on both legs and the discounting curve is used as the discounting
#     curve for both legs.
#
#     If three curves are given the single discounting curve is used as the
#     discounting curve for both legs.
#     """
#
#     if fx is None:
#         if solver is None:
#             fx_ = None
#             # fx_ = 1.0
#         elif solver is not None:
#             if solver.fx is None:
#                 fx_ = None
#                 # fx_ = 1.0
#             else:
#                 fx_ = solver.fx
#     else:
#         fx_ = fx
#
#     if curves is None:
#         return (None, None, None, None), fx_
#
#     if isinstance(curves, (Curve, str)):
#         curves = [curves]
#     if solver is None:
#         def check_curve(curve):
#             if isinstance(curve, str):
#                 raise ValueError(
#                     "`curves` must contain Curve, not str, if `solver` not given."
#                 )
#             return curve
#         curves_ = tuple(check_curve(curve) for curve in curves)
#     else:
#         try:
#             curves_ = tuple(_get_curve_from_solver(curve, solver) for curve in curves)
#         except KeyError:
#             raise ValueError(
#                 "`curves` must contain str curve `id` s existing in `solver` "
#                 "(or its associated `pre_solvers`)"
#             )
#
#     if len(curves_) == 1:
#         curves_ *= 4
#     elif len(curves_) == 2:
#         curves_ *= 2
#     elif len(curves_) == 3:
#         curves_ += (curves_[1],)
#     elif len(curves_) > 4:
#         raise ValueError("Can only supply a maximum of 4 `curves`.")
#
#     return curves_, fx_


class Sensitivities:
    """
    Base class to add risk sensitivity calculations to an object with an ``npv()``
    method.
    """
    def delta(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Calculate delta risk against the calibrating instruments of the
        :class:`~rateslib.curves.Curve`.

        Parameters
        ----------
        curves : Curve, str or list of such, optional
            A single :class:`~rateslib.curves.Curve` or id or a list of such.
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for ``leg1``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg1``.
            - Forecasting :class:`~rateslib.curves.Curve` for ``leg2``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg2``.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that constructs
            :class:`~rateslib.curves.Curve` from calibrating
            instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            :class:`~rateslib.fx.FXRates` or :class:`~rateslib.fx.FXForwards` object,
            converts from local currency into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx_rate`` is an :class:`~rateslib.fx.FXRates` or
            :class:`~rateslib.fx.FXForwards` object.

        Returns
        -------
        DataFrame
        """
        if solver is None:
            raise ValueError("`solver` is required for delta/gamma methods.")
        npv = self.npv(curves, solver, fx, base)
        return solver.delta(npv)

    def gamma(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Calculate cross-gamma risk against the calibrating instruments of the
        :class:`~rateslib.curves.Curve`.

        Parameters
        ----------
        curves : Curve, str or list of such, optional
            A single :class:`~rateslib.curves.Curve` or id or a list of such.
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for ``leg1``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg1``.
            - Forecasting :class:`~rateslib.curves.Curve` for ``leg2``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg2``.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that constructs
            :class:`~rateslib.curves.Curve` from calibrating
            instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            :class:`~rateslib.fx.FXRates` or :class:`~rateslib.fx.FXForwards` object,
            converts from local currency into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx_rate`` is an :class:`~rateslib.fx.FXRates` or
            :class:`~rateslib.fx.FXForwards` object.

        Returns
        -------
        DataFrame
        """
        if solver is None:
            raise ValueError("`solver` is required for delta/gamma methods.")
        _ = solver._ad  # store original order
        solver._set_ad_order(2)
        npv = self.npv(curves, solver, fx, base)
        grad_s_sT_P = solver.gamma(npv)
        solver._set_ad_order(_)  # reset original order
        return grad_s_sT_P


class _AttributesMixin:
    _fixed_rate_mixin = False
    _float_spread_mixin = False
    _leg2_fixed_rate_mixin = False
    _leg2_float_spread_mixin = False

    @property
    def fixed_rate(self):
        """
        float or None : If set will also set the ``fixed_rate`` of the contained
        leg1.

        .. note::
           ``fixed_rate``, ``float_spread``, ``leg2_fixed_rate`` and
           ``leg2_float_spread`` are attributes only applicable to certain
           ``Instruments``. *AttributeErrors* are raised if calling or setting these
           is invalid.

        """
        return self._fixed_rate

    @fixed_rate.setter
    def fixed_rate(self, value):
        if not self._fixed_rate_mixin:
            raise AttributeError("Cannot set `fixed_rate` for this Instrument.")
        self._fixed_rate = value
        self.leg1.fixed_rate = value

    @property
    def leg2_fixed_rate(self):
        """
        float or None : If set will also set the ``fixed_rate`` of the contained
        leg2.
        """
        return self._leg2_fixed_rate

    @leg2_fixed_rate.setter
    def leg2_fixed_rate(self, value):
        if not self._leg2_fixed_rate_mixin:
            raise AttributeError("Cannot set `leg2_fixed_rate` for this Instrument.")
        self._leg2_fixed_rate = value
        self.leg2.fixed_rate = value

    @property
    def float_spread(self):
        """
        float or None : If set will also set the ``float_spread`` of contained
        leg1.
        """
        return self._float_spread

    @float_spread.setter
    def float_spread(self, value):
        if not self._float_spread_mixin:
            raise AttributeError("Cannot set `float_spread` for this Instrument.")
        self._float_spread = value
        self.leg1.float_spread = value
        # if getattr(self, "_float_mixin_leg", None) is None:
        #     self.leg1.float_spread = value
        # else:
        #     # allows fixed_rate and float_rate to exist simultaneously for diff legs.
        #     leg = getattr(self, "_float_mixin_leg", None)
        #     getattr(self, f"leg{leg}").float_spread = value

    @property
    def leg2_float_spread(self):
        """
        float or None : If set will also set the ``float_spread`` of contained
        leg2.
        """
        return self._leg2_float_spread

    @leg2_float_spread.setter
    def leg2_float_spread(self, value):
        if not self._leg2_float_spread_mixin:
            raise AttributeError("Cannot set `leg2_float_spread` for this Instrument.")
        self._leg2_float_spread = value
        self.leg2.float_spread = value

    def _get_curves_and_fx_maybe_from_solver(
        self,
        solver: Optional[Solver],
        curves: Optional[Union[Curve, str, list]],
        fx: Optional[Union[float, FXRates, FXForwards]],
    ):
        """
        Parses the ``solver``, ``curves`` and ``fx`` arguments in combination.

        Returns
        -------
        tuple : (leg1 forecasting, leg1 discounting, leg2 forecasting, leg2 discounting), fx

        Notes
        -----
        If only one curve is given this is used as all four curves.

        If two curves are given the forecasting curve is used as the forecasting
        curve on both legs and the discounting curve is used as the discounting
        curve for both legs.

        If three curves are given the single discounting curve is used as the
        discounting curve for both legs.
        """
        if fx is None:
            if solver is None:
                fx_ = None
                # fx_ = 1.0
            elif solver is not None:
                if solver.fx is None:
                    fx_ = None
                    # fx_ = 1.0
                else:
                    fx_ = solver.fx
        else:
            fx_ = fx

        if curves is None and getattr(self, "curves", None) is None:
            return (None, None, None, None), fx_
        elif curves is None:
            curves = self.curves

        if isinstance(curves, (Curve, str)):
            curves = [curves]
        if solver is None:
            def check_curve(curve):
                if isinstance(curve, str):
                    raise ValueError(
                        "`curves` must contain Curve, not str, if `solver` not given."
                    )
                return curve

            curves_ = tuple(check_curve(curve) for curve in curves)
        else:
            try:
                curves_ = tuple(
                    _get_curve_from_solver(curve, solver) for curve in curves)
            except KeyError:
                raise ValueError(
                    "`curves` must contain str curve `id` s existing in `solver` "
                    "(or its associated `pre_solvers`)"
                )

        if len(curves_) == 1:
            curves_ *= 4
        elif len(curves_) == 2:
            curves_ *= 2
        elif len(curves_) == 3:
            curves_ += (curves_[1],)
        elif len(curves_) > 4:
            raise ValueError("Can only supply a maximum of 4 `curves`.")

        return curves_, fx_


class Value(_AttributesMixin):
    """
    A null instrument which can be used within a :class:`~rateslib.solver.Solver`
    to directly parametrise a node.

    Parameters
    ----------
    effective : datetime
        The datetime index for which the `rate`, which is just the curve value, is
        returned.
    curves : Curve, LineCurve, str or list of such, optional
        A single :class:`~rateslib.curves.Curve`,
        :class:`~rateslib.curves.LineCurve` or id or a
        list of such. A list defines the following curves in the order:

        - Forecasting :class:`~rateslib.curves.Curve` or
          :class:`~rateslib.curves.LineCurve` for ``leg1``.
        - Discounting :class:`~rateslib.curves.Curve` for ``leg1``.
        - Forecasting :class:`~rateslib.curves.Curve` or
          :class:`~rateslib.curves.LineCurve` for ``leg2``.
        - Discounting :class:`~rateslib.curves.Curve` for ``leg2``.

    Examples
    --------
    The below :class:`~rateslib.curves.Curve` is solved directly
    from a calibrating DF value on 1st Nov 2022.

    .. ipython:: python

       curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 1.0}, id="v")
       instruments = [(Value(dt(2022, 11, 1)), (curve,), {})]
       solver = Solver([curve], instruments, [0.99])
       curve[dt(2022, 1, 1)]
       curve[dt(2022, 11, 1)]
       curve[dt(2023, 1, 1)]
    """

    def __init__(
        self,
        effective: datetime,
        curves: Optional[Union[list, str, Curve]] = None,
    ):
        self.effective = effective
        self.curves = curves

    def rate(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the forecasting :class:`~rateslib.curves.Curve` or
        :class:`~rateslib.curves.LineCurve` value on the ``effective`` date of the
        instrument.
        """
        curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        return curves[0][self.effective]


### Securities


class FixedRateBond(Sensitivities, _AttributesMixin):
    # TODO ensure calculations work for amortizing bonds.
    """
    Create a fixed rate bond security.

    Parameters
    ----------
    effective : datetime
        The adjusted or unadjusted effective date.
    termination : datetime or str
        The adjusted or unadjusted termination date. If a string, then a tenor must be
        given expressed in days (`"D"`), months (`"M"`) or years (`"Y"`), e.g. `"48M"`.
    frequency : str in {"M", "B", "Q", "T", "S", "A"}, optional
        The frequency of the schedule. "Z" is not permitted.
    stub : str combining {"SHORT", "LONG"} with {"FRONT", "BACK"}, optional
        The stub type to enact on the swap. Can provide two types, for
        example "SHORTFRONTLONGBACK".
    front_stub : datetime, optional
        An adjusted or unadjusted date for the first stub period.
    back_stub : datetime, optional
        An adjusted or unadjusted date for the back stub period.
        See notes for combining ``stub``, ``front_stub`` and ``back_stub``
        and any automatic stub inference.
    roll : int in [1, 31] or str in {"eom", "imm", "som"}, optional
        The roll day of the schedule. Inferred if not given.
    eom : bool, optional
        Use an end of month preference rather than regular rolls for inference. Set by
        default. Not required if ``roll`` is specified.
    modifier : str, optional
        The modification rule, in {"F", "MF", "P", "MP"}
    calendar : calendar or str, optional
        The holiday calendar object to use. If str, looks up named calendar from
        static data.
    payment_lag : int, optional
        The number of business days to lag payments by.
    notional : float, optional
        The leg notional, which is applied to each period.
    currency : str, optional
        The currency of the leg (3-digit code).
    amortization: float, optional
        The amount by which to adjust the notional each successive period. Should have
        sign equal to that of notional if the notional is to reduce towards zero.
    convention: str, optional
        The day count convention applied to calculations of period accrual dates.
        See :meth:`~rateslib.calendars.dcf`.
    fixed_rate : float, optional
        The **coupon** rate applied to determine cashflows. Can be set
        to `None` and designated
        later, perhaps after a mid-market rate for all periods has been calculated.
    ex_div : int
        The number of days prior to a cashflow during which the bond is considered
        ex-dividend.
    settle : int
        The number of business days for regular settlement time, i.e, 1 is T+1.

    Attributes
    ----------
    ex_div_days : int
    leg1 : FixedLegExchange
    """
    _fixed_rate_mixin = True

    def __init__(
        self,
        effective: datetime,
        termination: Union[datetime, str] = None,
        frequency: str = None,
        stub: Optional[str] = None,
        front_stub: Optional[datetime] = None,
        back_stub: Optional[datetime] = None,
        roll: Optional[Union[str, int]] = None,
        eom: Optional[bool] = None,
        modifier: Optional[str] = False,
        calendar: Optional[Union[CustomBusinessDay, str]] = None,
        payment_lag: Optional[int] = None,
        notional: Optional[float] = None,
        currency: Optional[str] = None,
        amortization: Optional[float] = None,
        convention: Optional[str] = None,
        fixed_rate: Optional[float] = None,
        ex_div: int = 0,
        settle: int = 1,
    ):
        if frequency.lower() == "z":
            raise ValueError("FixedRateBond `frequency` must be in {M, B, Q, T, S, A}.")
        if payment_lag is None:
            payment_lag = defaults.payment_lag_specific[type(self).__name__]
        self._fixed_rate = fixed_rate
        self.ex_div_days = ex_div
        self.settle = settle
        self.leg1 = FixedLegExchange(
            effective=effective,
            termination=termination,
            frequency=frequency,
            stub=stub,
            front_stub=front_stub,
            back_stub=back_stub,
            roll=roll,
            eom=eom,
            modifier=modifier,
            calendar=calendar,
            payment_lag=payment_lag,
            payment_lag_exchange=payment_lag,
            notional=notional,
            currency=currency,
            amortization=amortization,
            convention=convention,
            fixed_rate=fixed_rate,
            initial_exchange=False,
        )
        if self.leg1.amortization != 0:
            raise NotImplementedError("`amortization` for FixedRateBond must be zero.")

    def ex_div(self, settlement: datetime):
        """
        Return a boolean whether the security is ex-div on the settlement.

        Parameters
        ----------
        settlement : datetime
             The settlement date to test.

        Returns
        -------
        bool
        """
        prev_a_idx = index_left(
            self.leg1.schedule.aschedule,
            len(self.leg1.schedule.aschedule),
            settlement,
        )
        ex_div_date = add_tenor(
            self.leg1.schedule.aschedule[prev_a_idx+1],
            f"{-self.ex_div_days}B",
            None,  # modifier not required for business day tenor
            self.leg1.schedule.calendar,
        )
        return True if settlement >= ex_div_date else False

    def accrued(self, settlement: datetime):
        """
        Calculate the accrued amount per nominal par value of 100.

        Parameters
        ----------
        settlement : datetime
            The settlement date which to measure accrued interest against.

        Notes
        -----
        Fractionally apportions the coupon payment based on calendar days.

        .. math::

           \\text{Accrued} = \\text{Coupon} \\times \\frac{\\text{Settle - Last Coupon}}{\\text{Next Coupon - Last Coupon}}

        """
        # TODO validate against effective and termination?
        frac, acc_idx = self._accrued_frac(settlement)
        if self.ex_div(settlement):
            frac = (frac-1)  # accrued is negative in ex-div period
        return (
            frac * self.leg1.periods[acc_idx].cashflow / -self.leg1.notional * 100
        )

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.

    def _accrued_frac(self, settlement: datetime):
        """
        Return the accrual fraction of period between last coupon and settlement and
        coupon period left index
        """
        acc_idx = index_left(
            self.leg1.schedule.aschedule,
            len(self.leg1.schedule.aschedule),
            settlement,
        )
        return (
            (settlement - self.leg1.schedule.aschedule[acc_idx]) /
            (self.leg1.schedule.aschedule[acc_idx+1] -
             self.leg1.schedule.aschedule[acc_idx])
        ), acc_idx

    def _price_from_ytm(self, ytm: float, settlement: datetime, dirty: bool = False):
        """
        Loop through all future cashflows and discount them with ``ytm`` to achieve
        correct price.
        """
        # TODO note this formula does not account for back stubs
        # this is also mentioned in Coding IRs

        f = 12 / defaults.frequency_months[self.leg1.schedule.frequency]
        v = 1 / (1 + ytm / (100 * f))

        acc_frac, acc_idx = self._accrued_frac(settlement)
        if self.leg1.periods[acc_idx].stub:
            # is a stub so must account for discounting in a different way.
            fd0 = self.leg1.periods[acc_idx].dcf * f * (1 - acc_frac)
        else:
            fd0 = 1 - acc_frac

        d = 0
        for i, p_idx in enumerate(range(acc_idx, len(self.leg1.schedule.aschedule)-1)):
            if i == 0 and self.ex_div(settlement):
                continue
            else:
                d += self.leg1.periods[p_idx].cashflow * v ** i
        d += self.leg1.periods[-1].cashflow * v ** i
        p = v**fd0 * d / -self.leg1.notional * 100
        return p if dirty else p - self.accrued(settlement)

    def price(self, ytm: float, settlement: datetime, dirty: bool = False):
        """
        Calculate the price of the security per nominal value of 100.

        Parameters
        ----------
        ytm : float
            The yield-to-maturity against which to determine the price.
        settlement : datetime
            The settlement date on which to determine the price.
        dirty : bool, optional
            If `True` will include the
            :meth:`rateslib.instruments.FixedRateBond.accrued` in the price.

        Returns
        -------
        float, Dual, Dual2

        Examples
        --------
        This example is taken from the UK debt management office website.
        The result should be `141.070132` and the bond is ex-div.

        .. ipython:: python

           gilt = FixedRateBond(
               effective=dt(1998, 12, 7),
               termination=dt(2015, 12, 7),
               frequency="S",
               calendar="ldn",
               currency="gbp",
               convention="ActActICMA",
               ex_div=7,
               fixed_rate=8.0
           )
           gilt.ex_div(dt(1999, 5, 27))
           gilt.price(
               ytm=4.445,
               settlement=dt(1999, 5, 27),
               dirty=True
           )

        This example is taken from the Swedish national debt office website.
        The result of accrued should, apparently, be `0.210417` and the clean
        price should be `99.334778`.

        .. ipython:: python

           bond = FixedRateBond(
               effective=dt(2017, 5, 12),
               termination=dt(2028, 5, 12),
               frequency="A",
               calendar="stk",
               currency="sek",
               convention="ActActICMA",
               ex_div=5,
               fixed_rate=0.75
           )
           bond.ex_div(dt(2017, 8, 23))
           bond.accrued(dt(2017, 8, 23))
           bond.price(
               ytm=0.815,
               settlement=dt(2017, 8, 23),
               dirty=False
           )

        """
        return self._price_from_ytm(ytm, settlement, dirty)

    def duration(self, ytm: float, settlement: datetime, metric: str = "risk"):
        """
        Return the (negated) derivative of ``price`` w.r.t. ``ytm``.

        Parameters
        ----------
        ytm : float
            The yield-to-maturity for the bond.
        settlement : datetime
            The settlement date of the bond.
        metric : str
            The specific duration calculation to return. See notes.

        Returns
        -------
        float

        Notes
        -----
        The available metrics are:

        - *"risk"*: the derivative of price w.r.t. ytm, scaled to -1bp.

          .. math::

             risk = - \\frac{\partial P }{\partial y}

        - *"modified"*: the modified duration which is *risk* divided by price.

          .. math::

             mduration = \\frac{risk}{P} = - \\frac{1}{P} \\frac{\partial P }{\partial y}

        - *"duration"*: the duration which is modified duration reverse modified.

          .. math::

             duration = mduration \\times (1 + y / f)

        Examples
        --------
        .. ipython:: python

           gilt = FixedRateBond(
               effective=dt(1998, 12, 7),
               termination=dt(2015, 12, 7),
               frequency="S",
               calendar="ldn",
               currency="gbp",
               convention="ActActICMA",
               ex_div=7,
               fixed_rate=8.0
           )
           gilt.duration(4.445, dt(1999, 5, 27), "risk")
           gilt.duration(4.445, dt(1999, 5, 27), "modified")
           gilt.duration(4.445, dt(1999, 5, 27), "duration")

        This result is interpreted as cents. If the yield is increased by 1bp the price
        will fall by 14.65 cents.

        .. ipython:: python

           gilt.price(4.445, dt(1999, 5, 27))
           gilt.price(4.455, dt(1999, 5, 27))
        """
        if metric == "risk":
            _ = -self.price(Dual(float(ytm), "y"), settlement).gradient("y")[0]
        elif metric == "modified":
            price = -self.price(Dual(float(ytm), "y"), settlement, dirty=True)
            _ = -price.gradient("y")[0] / float(price) * 100
        elif metric == "duration":
            price = -self.price(Dual(float(ytm), "y"), settlement, dirty=True)
            f = 12 / defaults.frequency_months[self.leg1.schedule.frequency]
            v = (1 + float(ytm) / (100 * f))
            _ = -price.gradient("y")[0] / float(price) * v * 100
        return _

    def convexity(self, ytm: float, settlement: datetime):
        """
        Return the second derivative of ``price`` w.r.t. ``ytm``.

        Parameters
        ----------
        ytm : float
            The yield-to-maturity for the bond.
        settlement : datetime
            The settlement date of the bond.

        Returns
        -------
        float

        Examples
        --------
        .. ipython:: python

           gilt = FixedRateBond(
               effective=dt(1998, 12, 7),
               termination=dt(2015, 12, 7),
               frequency="S",
               calendar="ldn",
               currency="gbp",
               convention="ActActICMA",
               ex_div=7,
               fixed_rate=8.0
           )
           gilt.convexity(4.445, dt(1999, 5, 27))

        This number is interpreted as hundredths of a cent. For a 1bp increase in
        yield the duration will decrease by 2 hundredths of a cent.

        .. ipython:: python

           gilt.duration(4.445, dt(1999, 5, 27))
           gilt.duration(4.455, dt(1999, 5, 27))
        """
        return self.price(Dual2(float(ytm), "y"), settlement).gradient("y", 2)[0][0]

    def ytm(self, price: float, settlement: datetime, dirty: bool = False):
        """
        Calculate the yield-to-maturity of the security given its price.

        Parameters
        ----------
        price : float
            The price, per 100 nominal, against which to determine the yield.
        settlement : datetime
            The settlement date on which to determine the price.
        dirty : bool, optional
            If `True` will assume the
            :meth:`~rateslib.instruments.FixedRateBond.accrued` is included in the price.

        Returns
        -------
        float, Dual, Dual2

        Notes
        -----
        If ``price`` is given as :class:`~rateslib.dual.Dual` or
        :class:`~rateslib.dual.Dual2` input the result of the yield will be output
        as the same type with the variables passed through accordingly.

        Examples
        --------
        .. ipython:: python

           gilt = FixedRateBond(
               effective=dt(1998, 12, 7),
               termination=dt(2015, 12, 7),
               frequency="S",
               calendar="ldn",
               currency="gbp",
               convention="ActActICMA",
               ex_div=7,
               fixed_rate=8.0
           )
           gilt.ytm(
               price=141.0701315,
               settlement=dt(1999,5,27),
               dirty=True
           )
           gilt.ytm(Dual(141.0701315, ["price", "a", "b"], [1, -0.5, 2]), dt(1999, 5, 27), True)
           gilt.ytm(Dual2(141.0701315, ["price", "a", "b"], [1, -0.5, 2]), dt(1999, 5, 27), True)

        """

        def root(y):
            return self._price_from_ytm(y, settlement, dirty) - price
        x = brentq(root, -99, 10000)

        if isinstance(price, Dual):
            # use the inverse function theorem to express x as a Dual
            p = self._price_from_ytm(Dual(x, "y"), settlement, dirty)
            return Dual(x, price.vars, 1 / p.gradient("y")[0] * price.dual)
        elif isinstance(price, Dual2):
            # use the IFT in 2nd order to express x as a Dual2
            p = self._price_from_ytm(Dual2(x, "y"), settlement, dirty)
            dydP = 1 / p.gradient("y")[0]
            d2ydP2 = - p.gradient("y", order=2)[0][0] * p.gradient("y")[0] ** -3
            return Dual2(
                x,
                price.vars,
                dydP * price.dual,
                0.5 * (dydP * price.gradient(price.vars, order=2) +
                d2ydP2 * np.matmul(price.dual[:, None], price.dual[None, :]))
            )
        else:
            return x

    def rate(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
        metric="dirty_price",
    ):
        """
        Return various pricing metrics of the security calculated from
        :class:`~rateslib.curves.Curve` s.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`Curve` or id or a list of such. A list defines the
            following curves in the order:

              - Forecasting :class:`Curve` for ``leg1``.
              - Discounting :class:`Curve` for ``leg1``.
        solver : Solver, optional
            The numerical :class:`Solver` that constructs ``Curves`` from calibrating
            instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            ``FXRates`` or ``FXForwards`` object, converts from local currency
            into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx`` is an ``FXRates`` or ``FXForwards`` object.
        metric : str in {"dirty_price", "clean_price", "ytm"}, optional
            Metric returned by the method.

        Returns
        -------
        float, Dual, Dual2
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        settlement = add_tenor(
            curves[1].node_dates[0], f"{self.settle}B", None, self.leg1.schedule.calendar
        )
        npv = self.npv(curves, solver, fx, base)

        # scale price to par 100 and make a fwd adjustment according to curve
        dirty_price = npv * 100 / (-self.leg1.notional * curves[1][settlement])

        if metric == "dirty_price":
            return dirty_price
        elif metric == "clean_price":
            return dirty_price - self.accrued(settlement)
        elif metric == "ytm":
            return self.ytm(dirty_price, settlement, True)
        raise ValueError("`metric` must be in {'dirty_price', 'clean_price', 'ytm'}.")

    def cashflows(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
        settlement: datetime = None
    ):
        """
        Return the properties of the security used in calculating cashflows.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`Curve` or id or a list of such. A list defines the
            following curves in the order:

              - Forecasting :class:`Curve` for ``leg1``.
              - Discounting :class:`Curve` for ``leg1``.
        solver : Solver, optional
            The numerical :class:`Solver` that constructs ``Curves`` from calibrating
            instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            ``FXRates`` or ``FXForwards`` object, converts from local currency
            into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx_rate`` is an ``FXRates`` or ``FXForwards`` object.
        settlement : datetime, optional
            The settlement date of the security. If *None* adds the regular ``settle``
            time to the initial node date of the given discount ``curves``.

        Returns
        -------
        DataFrame
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        if settlement is None:
            settlement = add_tenor(
                curves[1].node_dates[0], f"{self.settle}B", None, self.leg1.schedule.calendar
            )
        cashflows = self.leg1.cashflows(curves[0], curves[1], fx, base)
        if self.ex_div(settlement):
            # deduct the next coupon which has otherwise been included in valuation
            current_period = index_left(
                self.leg1.schedule.aschedule,
                self.leg1.schedule.n_periods + 1,
                settlement,
            )
            cashflows.loc[current_period, defaults.headers["npv"]] = 0
            cashflows.loc[current_period, defaults.headers["npv_fx"]] = 0
        return cashflows

    def npv(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the NPV of the security by summing cashflow valuations.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`Curve` or id or a list of such. A list defines the
            following curves in the order:

              - Forecasting :class:`Curve` for ``leg1``.
              - Discounting :class:`Curve` for ``leg1``.
        solver : Solver, optional
            The numerical :class:`Solver` that constructs ``Curves`` from calibrating
            instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            ``FXRates`` or ``FXForwards`` object, converts from local currency
            into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx`` is an ``FXRates`` or ``FXForwards`` object.

        Returns
        -------
        float or Dual

        Notes
        -----
        The ``settlement`` date of the bond is inferred from the objects ``settle``
        days parameter and the initial date of the supplied ``curves``.
        The NPV returned is for immediate settlement.

        If **only one curve** is given this is used as all four curves.

        If **two curves** are given the forecasting curve is used as the forecasting
        curve on both legs and the discounting curve is used as the discounting
        curve for both legs.
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        settlement = add_tenor(
            curves[1].node_dates[0], f"{self.settle}B", None, self.leg1.schedule.calendar
        )
        npv = self.leg1.npv(curves[0], curves[1], fx, base)
        if self.ex_div(settlement):
            # deduct the next coupon which has otherwise been included in valuation
            current_period = index_left(
                self.leg1.schedule.aschedule,
                self.leg1.schedule.n_periods + 1,
                settlement,
            )
            npv -= self.leg1.periods[current_period].npv(
                curves[0], curves[1], fx, base
            )
        return npv

    def analytic_delta(
        self,
        curve: Optional[Curve] = None,
        disc_curve: Optional[Curve] = None,
        fx: Union[float, FXRates, FXForwards] = 1.0,
        base: Optional[str] = None,
    ):
        """
        Return the analytic delta of the security via summing all periods.

        Parameters
        ----------
        curve : Curve
            The forecasting curve object. Not used unless it is set equal to
            ``disc_curve``.
        disc_curve : Curve, optional
            The discounting curve object used in calculations.
            Set equal to ``curve`` if not given.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            :class:`~rateslib.fx.FXRates` or :class:`~rateslib.fx.FXForwards`
            object, converts from local currency into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx`` is an :class:`~rateslib.fx.FXRates` or
            :class:`~rateslib.fx.FXForwards` object.

        Returns
        -------
        float, Dual, Dual2
        """
        # TODO make this ex-div compliant
        disc_curve = disc_curve or curve
        settlement = add_tenor(
            disc_curve.node_dates[0], f"{self.settle}B", None,
            self.leg1.schedule.calendar
        )
        a_delta = self.leg1.analytic_delta(curve, disc_curve, fx, base)
        if self.ex_div(settlement):
            # deduct the next coupon which has otherwise been included in valuation
            current_period = index_left(
                self.leg1.schedule.aschedule,
                self.leg1.schedule.n_periods + 1,
                settlement,
            )
            a_delta -= self.leg1.periods[current_period].analytic_delta(
                curve, disc_curve, fx, base
            )
        return a_delta

    def forward_price(self, price, settlement, forward_settlement, disc_curve):
        """
        Calculate the forward price of the security.

        Parameters
        ----------
        price : float, Dual, Dual2
            The initial price of the security.
        settlement : datetime
            The settlement date associated with the ``price``.
        forward_settlement : datetime
            The forward settlement date for which to determine the price.
        disc_curve : Curve
            The rate which to discount cashflows, usually termed the repo rate.

        Returns
        -------
        float, Dual, Dual2

        Notes
        -----
        This calculation only rolls a bond price forward accroding to the repo rate.
        It does **not** account for cashflows or ex-dividend periods.
        """
        # TODO make this calculation accounting for forward historic coupons
        multiplier = disc_curve[settlement] / disc_curve[forward_settlement]
        return price * multiplier


    # def par_spread(self, *args, price, settlement, dirty, **kwargs):
    #     """
    #     The spread to the fixed rate added to value the security at par valued from
    #     the given :class:`~rateslib.curves.Curve` s.
    #
    #     Parameters
    #     ----------
    #     args: tuple
    #         Positional arguments to :meth:`~rateslib.periods.BasePeriod.npv`.
    #     price: float
    #         The price of the security.
    #     settlement : datetime
    #         The settlement date.
    #     dirty : bool
    #         Whether the price given includes accrued interest.
    #     kwargs : dict
    #         Keyword arguments to :meth:`~rateslib.periods.BasePeriod.npv`.
    #
    #     Returns
    #     -------
    #     float, Dual, Dual2
    #     """
    #     TODO: calculte this formula.
    #     return (self.notional - self.npv(*args, **kwargs)) / self.analytic_delta(*args, **kwargs)


class Bill(FixedRateBond):

    def __init__(
            self,
            effective: datetime,
            termination: Union[datetime, str] = None,
            frequency: str = None,
            modifier: Optional[str] = False,
            calendar: Optional[Union[CustomBusinessDay, str]] = None,
            payment_lag: Optional[int] = None,
            notional: Optional[float] = None,
            currency: Optional[str] = None,
            convention: Optional[str] = None,
            settle: int = 1,
    ):
        if payment_lag is None:
            payment_lag = defaults.payment_lag_specific[type(self).__name__]
        super().__init__(
            effective=effective,
            termination=termination,
            frequency=frequency,
            stub=None,
            front_stub=None,
            back_stub=None,
            roll=None,
            eom=None,
            modifier=modifier,
            calendar=calendar,
            payment_lag=payment_lag,
            notional=notional,
            currency=currency,
            amortization=None,
            convention=convention,
            fixed_rate=0,
            ex_div=0,
            settle=settle,
        )

    def rate(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
        metric="price",
    ):
        """
        Return various pricing metrics of the security calculated from
        :class:`~rateslib.curves.Curve` s.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`Curve` or id or a list of such. A list defines the
            following curves in the order:

              - Forecasting :class:`Curve` for ``leg1``.
              - Discounting :class:`Curve` for ``leg1``.
        solver : Solver, optional
            The numerical :class:`Solver` that constructs ``Curves`` from calibrating
            instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            ``FXRates`` or ``FXForwards`` object, converts from local currency
            into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code), set by default.
            Only used if ``fx`` is an ``FXRates`` or ``FXForwards`` object.
        metric : str in {"price", "discount_rate", "ytm", "simple_rate"}
            Metric returned by the method.

        Returns
        -------
        float, Dual, Dual2
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        settlement = add_tenor(
            curves[1].node_dates[0], f"{self.settle}B", None,
            self.leg1.schedule.calendar
        )
        # scale price to par 100 and make a fwd adjustment according to curve
        price = self.npv(curves, solver, fx, base) * 100 / \
            (-self.leg1.notional * curves[1][settlement])
        if metric == "price":
            return price
        elif metric == "discount_rate":
            return self.discount_rate(price, settlement)
        elif metric == "simple_rate":
            return self.simple_rate(price, settlement)
        elif metric == "ytm":
            return self.ytm(price, settlement, False)
        raise ValueError(
            "`metric` must be in {'price', 'discount_rate', 'ytm', 'simple_rate'}"
        )

    def simple_rate(self, price, settlement):
        dcf = (1 - self._accrued_frac(settlement)[0]) * self.leg1.periods[0].dcf
        return ((100 / price - 1) / dcf) * 100

    def discount_rate(self, price, settlement):
        dcf = (1 - self._accrued_frac(settlement)[0]) * self.leg1.periods[0].dcf
        rate = ((1 - price / 100) / dcf) * 100
        return rate

    def price(self, discount_rate, settlement):
        """
        Return the price of the bill given the ``discount_rate``.

        Parameters
        ----------
        discount_rate : float
            The rate used by the pricing formula.
        settlement : datetime
            The settlement date.

        Returns
        -------
        float, Dual, Dual2
        """
        dcf = (1 - self._accrued_frac(settlement)[0]) * self.leg1.periods[0].dcf
        return 100 - discount_rate * dcf

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.


class FloatRateBond(Sensitivities, _AttributesMixin):
    """
    Create a floating rate bond security.

    Parameters
    ----------
    effective : datetime
        The adjusted or unadjusted effective date.
    termination : datetime or str
        The adjusted or unadjusted termination date. If a string, then a tenor must be
        given expressed in days (`"D"`), months (`"M"`) or years (`"Y"`), e.g. `"48M"`.
    frequency : str in {"M", "B", "Q", "T", "S", "A"}, optional
        The frequency of the schedule. "Z" is not permitted.
    stub : str combining {"SHORT", "LONG"} with {"FRONT", "BACK"}, optional
        The stub type to enact on the swap. Can provide two types, for
        example "SHORTFRONTLONGBACK".
    front_stub : datetime, optional
        An adjusted or unadjusted date for the first stub period.
    back_stub : datetime, optional
        An adjusted or unadjusted date for the back stub period.
        See notes for combining ``stub``, ``front_stub`` and ``back_stub``
        and any automatic stub inference.
    roll : int in [1, 31] or str in {"eom", "imm", "som"}, optional
        The roll day of the schedule. Inferred if not given.
    eom : bool, optional
        Use an end of month preference rather than regular rolls for inference. Set by
        default. Not required if ``roll`` is specified.
    modifier : str, optional
        The modification rule, in {"F", "MF", "P", "MP"}
    calendar : calendar or str, optional
        The holiday calendar object to use. If str, looks up named calendar from
        static data.
    payment_lag : int, optional
        The number of business days to lag payments by.
    notional : float, optional
        The leg notional, which is applied to each period.
    currency : str, optional
        The currency of the leg (3-digit code).
    amortization: float, optional
        The amount by which to adjust the notional each successive period. Should have
        sign equal to that of notional if the notional is to reduce towards zero.
    convention: str, optional
        The day count convention applied to calculations of period accrual dates.
        See :meth:`~rateslib.calendars.dcf`.
    float_spread : float, optional
        The spread applied to determine cashflows. Can be set to `None` and designated
        later, perhaps after a mid-market spread for all periods has been calculated.
    spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    ex_div : int
        The number of days prior to a cashflow during which the bond is considered
        ex-dividend.
    settle : int
        The number of business days for regular settlement time, i.e, 1 is T+1.

    Notes
    -----
    .. warning::

       FRNs based on RFR rates which have ex-div days must ensure that fixings are
       available to define the entire period. This means that `ex_div` days must be less
       than the `fixing_method` `method_param` lag minus the time to settlement time.

        That is, a bond with a `method_param` of 5 and a settlement time of 2 days
        can have an `ex_div` period of at maximum 3.

        A bond with a `method_param` of 2 and a settlement time of 1 day cnan have an
        `ex_div` period of at maximum 1.

    Attributes
    ----------
    ex_div_days : int
    leg1 : FloatLegExchange
    """
    _float_spread_mixin = True

    def __init__(
        self,
        effective: datetime,
        termination: Union[datetime, str] = None,
        frequency: str = None,
        stub: Optional[str] = None,
        front_stub: Optional[datetime] = None,
        back_stub: Optional[datetime] = None,
        roll: Optional[Union[str, int]] = None,
        eom: Optional[bool] = None,
        modifier: Optional[str] = False,
        calendar: Optional[Union[CustomBusinessDay, str]] = None,
        payment_lag: Optional[int] = None,
        notional: Optional[float] = None,
        currency: Optional[str] = None,
        amortization: Optional[float] = None,
        convention: Optional[str] = None,
        float_spread: Optional[float] = None,
        fixings: Optional[Union[float, list]] = None,
        fixing_method: Optional[str] = None,
        method_param: Optional[int] = None,
        spread_compound_method: Optional[str] = None,
        ex_div: int = 0,
        settle: int = 1,
    ):
        if frequency.lower() == "z":
            raise ValueError("FloatRateBond `frequency` must be in {M, B, Q, T, S, A}.")
        if payment_lag is None:
            payment_lag = defaults.payment_lag_specific[type(self).__name__]
        self._float_spread = float_spread
        self.leg1 = FloatLegExchange(
            effective=effective,
            termination=termination,
            frequency=frequency,
            stub=stub,
            front_stub=front_stub,
            back_stub=back_stub,
            roll=roll,
            eom=eom,
            modifier=modifier,
            calendar=calendar,
            payment_lag=payment_lag,
            payment_lag_exchange=payment_lag,
            notional=notional,
            currency=currency,
            amortization=amortization,
            convention=convention,
            float_spread=float_spread,
            fixings=fixings,
            fixing_method=fixing_method,
            method_param=method_param,
            spread_compound_method=spread_compound_method,
            initial_exchange=False,
        )
        self.ex_div_days = ex_div
        self.settle = settle
        if "rfr" in self.leg1.fixing_method:
            if self.ex_div_days > self.leg1.method_param:
                raise ValueError(
                    "For RFR FRNs `ex_div` must be less than or equal to `method_param`"
                    " otherwise negative accrued payments cannot be explicitly "
                    "determined due to unknown fixings."
                )

    def ex_div(self, settlement: datetime):
        """
        Return a boolean whether the security is ex-div on the settlement.

        Parameters
        ----------
        settlement : datetime
             The settlement date to test.

        Returns
        -------
        bool
        """
        prev_a_idx = index_left(
            self.leg1.schedule.aschedule,
            len(self.leg1.schedule.aschedule),
            settlement,
        )
        ex_div_date = add_tenor(
            self.leg1.schedule.aschedule[prev_a_idx+1],
            f"{-self.ex_div_days}B",
            None,  # modifier not required for business day tenor
            self.leg1.schedule.calendar,
        )
        return True if settlement >= ex_div_date else False

    def _accrued_frac(self, settlement: datetime):
        """
        Return the accrual fraction of period between last coupon and settlement and
        coupon period left index
        """
        acc_idx = index_left(
            self.leg1.schedule.aschedule,
            len(self.leg1.schedule.aschedule),
            settlement,
        )
        return (
            (settlement - self.leg1.schedule.aschedule[acc_idx]) /
            (self.leg1.schedule.aschedule[acc_idx+1] -
             self.leg1.schedule.aschedule[acc_idx])
        ), acc_idx

    def accrued(self, settlement: datetime):
        """
        Calculate the accrued amount per nominal par value of 100.

        Parameters
        ----------
        settlement : datetime
            The settlement date which to measure accrued interest against.

        Notes
        -----
        If the coupon is IBOR based then the accrued
        fractionally apportions the coupon payment based on calendar days, including
        negative accrued during ex div periods.

        .. math::

           \\text{Accrued} = \\text{Coupon} \\times \\frac{\\text{Settle - Last Coupon}}{\\text{Next Coupon - Last Coupon}}

        If the coupon is based in RFR rates then the accrued is calculated upto the
        settlement date by compounding known fixing rates. Negative accrued is
        extrapolated by evaluating the number of remaining days in the ex div period
        and comparing them to the number of days in the existing accrual period.

        Examples
        --------
        An RFR based FRN where the fixings are known up to the end of period.

        .. ipython:: python

           fixings = Series(2.0, index=date_range(dt(1999, 12, 1), dt(2000, 6, 2)))
           frn = FloatRateBond(
               effective=dt(1998, 12, 7),
               termination=dt(2015, 12, 7),
               frequency="S",
               currency="gbp",
               convention="ActActICMA",
               ex_div=3,
               fixings=fixings,
               fixing_method="rfr_observation_shift",
               method_param=5,
           )
           frn.accrued(dt(2000, 3, 27))
           frn.accrued(dt(2000, 6, 4))


        An IBOR based FRN where the coupon is known in advance.

        .. ipython:: python

           fixings = Series(2.0, index=[dt(1999, 12, 5)])
           frn = FloatRateBond(
               effective=dt(1998, 12, 7),
               termination=dt(2015, 12, 7),
               frequency="S",
               currency="gbp",
               convention="ActActICMA",
               ex_div=7,
               fixings=fixings,
               fixing_method="ibor",
               method_param=2,
           )
           frn.accrued(dt(2000, 3, 27))
           frn.accrued(dt(2000, 6, 4))
        """
        # TODO validate against effective and termination?
        if self.leg1.fixing_method == "ibor":
            frac, acc_idx = self._accrued_frac(settlement)
            if self.ex_div(settlement):
                frac = (frac - 1)  # accrued is negative in ex-div period
            rate = self.leg1.periods[acc_idx].rate(
                Curve({
                    self.leg1.periods[acc_idx].start: 1.0,
                    self.leg1.periods[acc_idx].end: 1.0
                })
            )
            cashflow = -self.leg1.periods[acc_idx].notional * \
                       self.leg1.periods[acc_idx].dcf * \
                       rate / 100
            return (
                frac * cashflow / -self.leg1.notional * 100
            )
        else:  # is "rfr"
            acc_idx = index_left(
                self.leg1.schedule.aschedule,
                len(self.leg1.schedule.aschedule),
                settlement,
            )
            p = FloatPeriod(
                start=self.leg1.schedule.aschedule[acc_idx],
                end=settlement,
                payment=settlement,
                frequency=self.leg1.schedule.frequency,
                notional=-100,
                currency=self.leg1.currency,
                convention=self.leg1.convention,
                termination=self.leg1.schedule.aschedule[acc_idx + 1],
                stub=True,
                float_spread=self.float_spread,
                fixing_method=self.leg1.fixing_method,
                fixings=self.leg1.fixings[acc_idx],
                method_param=self.leg1.method_param,
                spread_compound_method=self.leg1.spread_compound_method
            )

            _crv = Curve({
                self.leg1.periods[acc_idx].start: 1.0,
                self.leg1.periods[acc_idx].end: 1.0
            })
            rate_to_settle = float(p.rate(_crv))
            accrued_to_settle = 100 * p.dcf * rate_to_settle / 100

            if self.ex_div(settlement):
                rate_to_end = self.leg1.periods[acc_idx].rate(_crv)
                accrued_to_end = 100 * self.leg1.periods[acc_idx].dcf * rate_to_end / 100
                return accrued_to_settle - accrued_to_end
            else:
                return accrued_to_settle

    def rate(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
        settlement=None,
        metric="dirty_price",
    ):
        """
        TODO
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        npv = self.npv(curves, solver, fx, base, settlement)

        # scale price to par 100 and make a fwd adjustment according to curve
        dirty_price = npv * 100 / (-self.leg1.notional * curves[1][settlement])

        if metric == "dirty_price":
            return dirty_price
        elif metric == "clean_price":
            return dirty_price - self.accrued(settlement)
        elif metric == "spread":
            if "rfr" in self.leg1.fixing_method and \
                    self.leg1.spread_compound_method != "none_simple":
                # This code replicates BaseLeg._spread for an FRN accounting for ex-div
                # via FRN.npv().

                _fs = self.float_spread
                self.float_spread = Dual2(0. if _fs is None else float(_fs), "spread_z")

                fore_curve, disc_curve = curves[0], curves[1]

                fore_ad = fore_curve.ad
                fore_curve._set_ad_order(2)

                disc_ad = disc_curve.ad
                disc_curve._set_ad_order(2)

                if isinstance(fx, (FXRates, FXForwards)):
                    _fx = None if fx is None else fx._ad
                    fx._set_ad_order(2)

                npv = self.npv([fore_curve, disc_curve], None, fx, base, settlement)
                b = npv.gradient("spread_z", order=1)[0]
                a = 0.5 * npv.gradient("spread_z", order=2)[0][0]
                c = npv + self.leg1.notional

                _1 = -c / b
                if abs(a) > 1e-14:
                    _2 = (-b - (b**2 - 4*a*c)**0.5) / (2*a)
                    # _2a = (-b + (b**2 - 4*a*c)**0.5) / (2*a)  # alt quadratic soln
                    _ = _2
                else:
                    _ = _1
                _ += 0. if _fs is None else _fs

                self.float_spread = _fs
                fore_curve._set_ad_order(fore_ad)
                disc_curve._set_ad_order(disc_ad)
                if isinstance(fx, (FXRates, FXForwards)):
                    fx._set_ad_order(_fx)
                return set_order(_, disc_ad)  # use disc_ad: cred spread from disc curve
            else:
                # NPV calc is efficient and requires no additional ingenuity.
                _ = (npv + self.leg1.notional) / \
                    self.analytic_delta(curves[0], curves[1], fx, base)
                _ += self.float_spread
                return _
        raise ValueError(
            "`metric` must be in {'dirty_price', 'clean_price', 'spread'}."
        )

    def cashflows(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
        settlement: datetime = None,
    ):
        """
        TODO
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        if settlement is None:
            settlement = curves[1].node_dates[0]
        cashflows = self.leg1.cashflows(curves[0], curves[1], fx, base)
        if self.ex_div(settlement):
            # deduct the next coupon which has otherwise been included in valuation
            current_period = index_left(
                self.leg1.schedule.aschedule,
                self.leg1.schedule.n_periods + 1,
                settlement,
            )
            cashflows.loc[current_period, defaults.headers["npv"]] = 0
            cashflows.loc[current_period, defaults.headers["npv_fx"]] = 0
        return cashflows

    def npv(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
        settlement: datetime = None
    ):
        """
        TODO
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        if settlement is None:
            settlement = curves[1].node_dates[0]
        npv = self.leg1.npv(curves[0], curves[1], fx, base)
        if self.ex_div(settlement):
            # deduct the next coupon which has otherwise been included in valuation
            current_period = index_left(
                self.leg1.schedule.aschedule,
                self.leg1.schedule.n_periods + 1,
                settlement,
            )
            npv -= self.leg1.periods[current_period].npv(
                curves[0], curves[1], fx, base
            )
        return npv

    def analytic_delta(self, *args, **kwargs):
        # TODO make this ex-div compliant
        return self.leg1.analytic_delta(*args, **kwargs)


### Single currency derivatives


class BaseDerivative(Sensitivities, _AttributesMixin, metaclass=ABCMeta):
    """
    Abstract base class with common parameters for many ``Derivative`` subclasses.

    Parameters
    ----------
    effective : datetime
        The adjusted or unadjusted effective date.
    termination : datetime or str
        The adjusted or unadjusted termination date. If a string, then a tenor must be
        given expressed in days (`"D"`), months (`"M"`) or years (`"Y"`), e.g. `"48M"`.
    frequency : str in {"M", "B", "Q", "T", "S", "A", "Z"}, optional
        The frequency of the schedule.
    stub : str combining {"SHORT", "LONG"} with {"FRONT", "BACK"}, optional
        The stub type to enact on the swap. Can provide two types, for
        example "SHORTFRONTLONGBACK".
    front_stub : datetime, optional
        An adjusted or unadjusted date for the first stub period.
    back_stub : datetime, optional
        An adjusted or unadjusted date for the back stub period.
        See notes for combining ``stub``, ``front_stub`` and ``back_stub``
        and any automatic stub inference.
    roll : int in [1, 31] or str in {"eom", "imm", "som"}, optional
        The roll day of the schedule. Inferred if not given.
    eom : bool, optional
        Use an end of month preference rather than regular rolls for inference. Set by
        default. Not required if ``roll`` is specified.
    modifier : str, optional
        The modification rule, in {"F", "MF", "P", "MP"}
    calendar : calendar or str, optional
        The holiday calendar object to use. If str, looks up named calendar from
        static data.
    payment_lag : int, optional
        The number of business days to lag payments by.
    notional : float, optional
        The leg notional, which is applied to each period.
    amortization: float, optional
        The amount by which to adjust the notional each successive period. Should have
        sign equal to that of notional if the notional is to reduce towards zero.
    convention: str, optional
        The day count convention applied to calculations of period accrual dates.
        See :meth:`~rateslib.calendars.dcf`.
    leg2_kwargs: Any
        All ``leg2`` arguments can be similarly input as above, e.g. ``leg2_frequency``.
        If **not** given, any ``leg2``
        argument inherits its value from the ``leg1`` arguments, except in the case of
        ``notional`` and ``amortization`` where ``leg2`` inherits the negated value.
    curves : Curve, LineCurve, str or list of such, optional
        A single :class:`~rateslib.curves.Curve`,
        :class:`~rateslib.curves.LineCurve` or id or a
        list of such. A list defines the following curves in the order:

        - Forecasting :class:`~rateslib.curves.Curve` or
          :class:`~rateslib.curves.LineCurve` for ``leg1``.
        - Discounting :class:`~rateslib.curves.Curve` for ``leg1``.
        - Forecasting :class:`~rateslib.curves.Curve` or
          :class:`~rateslib.curves.LineCurve` for ``leg2``.
        - Discounting :class:`~rateslib.curves.Curve` for ``leg2``.

    Attributes
    ----------
    effective : datetime
    termination : datetime
    frequency : str
    stub : str
    front_stub : datetime
    back_stub : datetime
    roll : str, int
    eom : bool
    modifier : str
    calendar : Calendar
    payment_lag : int
    notional : float
    amortization : float
    convention : str
    leg2_effective : datetime
    leg2_termination : datetime
    leg2_frequency : str
    leg2_stub : str
    leg2_front_stub : datetime
    leg2_back_stub : datetime
    leg2_roll : str, int
    leg2_eom : bool
    leg2_modifier : str
    leg2_calendar : Calendar
    leg2_payment_lag : int
    leg2_notional : float
    leg2_amortization : float
    leg2_convention : str
    """

    @abc.abstractmethod
    def __init__(
        self,
        effective: datetime,
        termination: Union[datetime, str] = None,
        frequency: Optional[int] = None,
        stub: Optional[str] = None,
        front_stub: Optional[datetime] = None,
        back_stub: Optional[datetime] = None,
        roll: Optional[Union[str, int]] = None,
        eom: Optional[bool] = None,
        modifier: Optional[str] = False,
        calendar: Optional[Union[CustomBusinessDay, str]] = None,
        payment_lag: Optional[int] = None,
        notional: Optional[float] = None,
        currency: Optional[str] = None,
        amortization: Optional[float] = None,
        convention: Optional[str] = None,
        leg2_effective: Optional[datetime] = "inherit",
        leg2_termination: Optional[Union[datetime, str]] = "inherit",
        leg2_frequency: Optional[int] = "inherit",
        leg2_stub: Optional[str] = "inherit",
        leg2_front_stub: Optional[datetime] ="inherit",
        leg2_back_stub: Optional[datetime] = "inherit",
        leg2_roll: Optional[Union[str, int]] = "inherit",
        leg2_eom: Optional[bool] = "inherit",
        leg2_modifier: Optional[str] = "inherit",
        leg2_calendar: Optional[Union[CustomBusinessDay, str]] = "inherit",
        leg2_payment_lag: Optional[int] = "inherit",
        leg2_notional: Optional[float] = "inherit_negate",
        leg2_currency: Optional[str] = "inherit",
        leg2_amortization: Optional[float] = "inherit_negate",
        leg2_convention: Optional[str] = "inherit",
        curves: Optional[Union[list, str, Curve]] = None,
    ):
        self.curves = curves
        notional = defaults.notional if notional is None else notional
        if payment_lag is None:
            payment_lag = defaults.payment_lag_specific[type(self).__name__]
        for attribute in [
            "effective", "termination", "frequency", "stub", "front_stub",
            "back_stub", "roll", "eom", "modifier", "calendar", "payment_lag",
            "convention", "notional", "amortization", "currency",
        ]:
            leg2_val, val = vars()[f"leg2_{attribute}"], vars()[attribute]
            if leg2_val == "inherit":
                _ = val
            elif leg2_val == "inherit_negate":
                _ = None if val is None else val * -1
            else:
                _ = leg2_val
            setattr(self, attribute, val)
            setattr(self, f"leg2_{attribute}", _)

    def analytic_delta(self, *args, leg=1, **kwargs):
        """
        Return the analytic delta of a leg of the derivative object.

        Parameters
        ----------
        args :
            Required positional arguments supplied to
            :meth:`BaseLeg.analytic_delta<rateslib.legs.BaseLeg.analytic_delta>`.
        leg : int in [1, 2]
            The leg identifier of which to take the analytic delta.
        kwargs :
            Required Keyword arguments supplied to
            :meth:`BaseLeg.analytic_delta()<rateslib.legs.BaseLeg.analytic_delta>`.

        Returns
        -------
        float, Dual, Dual2

        Examples
        --------
        .. ipython:: python

           curve = Curve({dt(2021,1,1): 1.00, dt(2025,1,1): 0.83}, "log_linear", id="SONIA")
           fxr = FXRates({"gbpusd": 1.25}, base="usd")

        .. ipython:: python

           irs = IRS(
               effective=dt(2022, 1, 1),
               termination="6M",
               frequency="Q",
               currency="gbp",
               notional=1e9,
               fixed_rate=5.0,
           )
           irs.analytic_delta(curve, curve)
           irs.analytic_delta(curve, curve, fxr)
           irs.analytic_delta(curve, curve, fxr, "gbp")
        """
        return getattr(self, f"leg{leg}").analytic_delta(*args, **kwargs)

    @abstractmethod
    def cashflows(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the properties of all legs used in calculating cashflows.

        Parameters
        ----------
        curves : Curve, LineCurve, str or list of such, optional
            A single :class:`~rateslib.curves.Curve`,
            :class:`~rateslib.curves.LineCurve` or id or a
            list of such. A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` or
              :class:`~rateslib.curves.LineCurve` for ``leg1``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg1``.
            - Forecasting :class:`~rateslib.curves.Curve` or
              :class:`~rateslib.curves.LineCurve` for ``leg2``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg2``.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that constructs
            ``Curves`` from calibrating instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            :class:`~rateslib.fx.FXRates` or :class:`~rateslib.fx.FXForwards` object,
            converts from local currency into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code).
            Only used if ``fx`` is an :class:`~rateslib.fx.FXRates` or
            :class:`~rateslib.fx.FXForwards` object. If not given defaults
            to ``fx.base``.

        Returns
        -------
        DataFrame

        Notes
        -----
        If **only one curve** is given this is used as all four curves.

        If **two curves** are given the forecasting curve is used as the forecasting
        curve on both legs and the discounting curve is used as the discounting
        curve for both legs.

        If **three curves** are given the single discounting curve is used as the
        discounting curve for both legs.

        Examples
        --------
        .. ipython:: python

           irs.cashflows([curve], None, fxr)
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        return concat([
            self.leg1.cashflows(curves[0], curves[1], fx, base),
            self.leg2.cashflows(curves[2], curves[3], fx, base),
            ], keys=["leg1", "leg2"],
        )

    @abc.abstractmethod
    def npv(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the NPV of the derivative object by summing legs.

        Parameters
        ----------
        curves : Curve, LineCurve, str or list of such
            A single :class:`~rateslib.curves.Curve`,
            :class:`~rateslib.curves.LineCurve` or id or a
            list of such. A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` or
              :class:`~rateslib.curves.LineCurve` for ``leg1``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg1``.
            - Forecasting :class:`~rateslib.curves.Curve` or
              :class:`~rateslib.curves.LineCurve` for ``leg2``.
            - Discounting :class:`~rateslib.curves.Curve` for ``leg2``.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that constructs
            ``Curves`` from calibrating instruments.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            :class:`~rateslib.fx.FXRates` or :class:`~rateslib.fx.FXForwards` object,
            converts from local currency into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3-digit code).
            Only used if ``fx`` is an :class:`~rateslib.fx.FXRates` or
            :class:`~rateslib.fx.FXForwards` object. If not given defaults
            to ``fx.base``.

        Returns
        -------
        float, Dual or Dual2

        Notes
        -----
        If **only one curve** is given this is used as all four curves.

        If **two curves** are given the forecasting curve is used as the forecasting
        curve on both legs and the discounting curve is used as the discounting
        curve for both legs.

        If **three curves** are given the single discounting curve is used as the
        discounting curve for both legs.

        Examples
        --------
        .. ipython:: python

           irs.npv(curve)
           irs.npv([curve], None, fxr)
           irs.npv([curve], None, fxr, "gbp")
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        leg1_npv = self.leg1.npv(curves[0], curves[1], fx, base)
        leg2_npv = self.leg2.npv(curves[2], curves[3], fx, base)
        return leg1_npv + leg2_npv

    @abc.abstractmethod
    def rate(self, *args, **kwargs):
        """
        Return the `rate` or typical `price` for a derivative instrument.

        Returns
        -------
        Dual

        Notes
        -----
        This method must be implemented for instruments to function effectively in
        :class:`Solver` iterations.
        """
        pass  # pragma: no cover

    # def delta(
    #     self,
    #     curves: Union[Curve, str, list],
    #     solver: Solver,
    #     fx: Optional[Union[float, FXRates, FXForwards]] = None,
    #     base: Optional[str] = None,
    # ):
    #     npv = self.npv(curves, solver, fx, base)
    #     return solver.delta(npv)
    #
    # def gamma(
    #     self,
    #     curves: Union[Curve, str, list],
    #     solver: Solver,
    #     fx: Optional[Union[float, FXRates, FXForwards]] = None,
    #     base: Optional[str] = None,
    # ):
    #     _ = solver._ad  # store original order
    #     solver._set_ad_order(2)
    #     npv = self.npv(curves, solver, fx, base)
    #     grad_s_sT_P = solver.gamma(npv)
    #     solver._set_ad_order(_)  # reset original order
    #     return grad_s_sT_P


class IRS(BaseDerivative):
    """
    Create an interest rate swap composing a :class:`~rateslib.legs.FixedLeg`
    and a :class:`~rateslib.legs.FloatLeg`.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fixed_rate : float or None
        The fixed rate applied to the :class:`~rateslib.legs.FixedLeg`. If `None`
        will be set to mid-market when curves are provided.
    leg2_float_spread : float, optional
        The spread applied to the :class:`~rateslib.legs.FloatLeg`. Can be set to
        `None` and designated
        later, perhaps after a mid-market spread for all periods has been calculated.
    leg2_spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    leg2_fixings : float, list, or Series optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given, is used as the first *m* RFR
        fixings for that :class:`~rateslib.periods.FloatPeriod`. If a datetime
        indexed ``Series`` will use the fixings that are available in that object,
        and derive the rest from the ``curve``.
    leg2_fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    leg2_method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.
    """
    _fixed_rate_mixin = True
    _leg2_float_spread_mixin = True

    def __init__(
        self,
        *args,
        fixed_rate: Optional[float] = None,
        leg2_float_spread: Optional[float] = None,
        leg2_spread_compound_method: Optional[str] = None,
        leg2_fixings: Optional[Union[float, list, Series]] = None,
        leg2_fixing_method: Optional[str] = None,
        leg2_method_param: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._fixed_rate = fixed_rate
        self._leg2_float_spread = leg2_float_spread
        self.leg1 = FixedLeg(
            fixed_rate=fixed_rate,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FloatLeg(
            float_spread=leg2_float_spread,
            spread_compound_method=leg2_spread_compound_method,
            fixings=leg2_fixings,
            fixing_method=leg2_fixing_method,
            method_param=leg2_method_param,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            notional=self.leg2_notional,
            currency=self.leg2_currency,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )

    def analytic_delta(self, *args, **kwargs):
        """
        Return the analytic delta of a leg of the derivative object.

        See :meth:`BaseDerivative.analytic_delta`.

        Examples
        --------
        .. ipython:: python

           forecasting_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.98})
           discounting_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.985})

        .. ipython:: python

           irs = IRS(
               effective=dt(2022, 2, 15),
               termination=dt(2022, 8, 15),
               frequency="Q",
               convention="30e360",
               leg2_convention="Act360",
               leg2_fixing_method="rfr_payment_delay",
               payment_lag=2,
               fixed_rate=2.50,
               notional=1e9,
               currency="gbp",
           )
           irs.analytic_delta(forecasting_curve, discounting_curve, leg=1)
        """
        return super().analytic_delta(*args, **kwargs)

    def npv(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the NPV of the derivative by summing legs.

        See :meth:`BaseDerivative.npv`.

        Examples
        --------
        .. ipython:: python

           irs.npv([forecasting_curve, discounting_curve])

        .. ipython:: python

           fxr = FXRates({"gbpusd": 2.0})
           irs.npv([forecasting_curve, discounting_curve], None, fxr, "usd")
        """
        if self.fixed_rate is None:
            # set a fixed rate for the purpose of pricing NPV, which should be zero.
            mid_market_rate = self.rate(curves, solver)
            self.leg1.fixed_rate = mid_market_rate.real
        return super().npv(curves, solver, fx, base)

    def rate(
        self,
        curves: Optional[Union[Curve, str, list]]=None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the mid-market rate of the IRS.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`~rateslib.curves.Curve` or id or a list of such.
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for floating leg.
            - Discounting :class:`~rateslib.curves.Curve` for both legs.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that
            constructs :class:`~rateslib.curves.Curve` from calibrating instruments.

            .. note::

               The arguments ``fx`` and ``base`` are unused by single currency
               derivatives rates calculations.

        Returns
        -------
        float, Dual or Dual2

        Notes
        -----
        The arguments ``fx`` and ``base`` are unused by single currency derivatives
        rates calculations.

        Examples
        --------
        .. ipython:: python

           irs.rate([forecasting_curve, discounting_curve])
        """
        curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        leg2_npv = self.leg2.npv(curves[2], curves[3])
        return self.leg1._spread(-leg2_npv, curves[0], curves[1]) / 100
        # leg1_analytic_delta = self.leg1.analytic_delta(curves[0], curves[1])
        # return leg2_npv / (leg1_analytic_delta * 100)

    def cashflows(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the properties of all legs used in calculating cashflows.

        See :meth:`BaseDerivative.cashflows`.

        Examples
        --------
        .. ipython:: python

           fxr = FXRates({"gbpusd": 2.0})
           irs.cashflows([forecasting_curve, discounting_curve], None, fxr, "usd")
        """
        return super().cashflows(curves, solver, fx, base)

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.

    def spread(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the mid-market float spread (bps) required to equate to the fixed rate.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`~rateslib.curves.Curve` or id or a list of such.
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for floating leg.
            - Discounting :class:`~rateslib.curves.Curve` for both legs.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that constructs
            :class:`~rateslib.curves.Curve` from calibrating instruments.

            .. note::

               The arguments ``fx`` and ``base`` are unused by single currency
               derivatives rates calculations.

        Returns
        -------
        float, Dual or Dual2

        Notes
        -----
        If the :class:`IRS` is specified without a ``fixed_rate`` this should always
        return the current ``leg2_float_spread`` value or zero since the fixed rate used
        for calculation is the implied rate including the current ``leg2_float_spread``
        parameter.

        Examples
        --------
        For the most common parameters this method will be exact.

        .. ipython:: python

           irs.spread([forecasting_curve, discounting_curve])
           irs.leg2_float_spread = 48.867036358702
           irs.npv([forecasting_curve, discounting_curve])

        When a non-linear spread compound method is used for float RFR legs this is
        an approximation, via second order Taylor expansion.

        .. ipython:: python

           irs = IRS(
               effective=dt(2022, 2, 15),
               termination=dt(2022, 8, 15),
               frequency="Q",
               convention="30e360",
               leg2_convention="Act360",
               leg2_fixing_method="rfr_payment_delay",
               leg2_spread_compound_method="isda_compounding",
               payment_lag=2,
               fixed_rate=2.50,
               leg2_float_spread=0,
               notional=50000000,
               currency="gbp",
           )
           irs.spread([forecasting_curve, discounting_curve])
           irs.leg2_float_spread = 48.59613590683196
           irs.npv([forecasting_curve, discounting_curve])
           irs.spread([forecasting_curve, discounting_curve])

        The ``leg2_float_spread`` is determined through NPV differences. If the difference
        is small since the defined spread is already quite close to the solution the
        approximation is much more accurate. This is shown above where the second call
        to ``irs.spread`` is different to the previous call.
        """
        irs_npv = self.npv(curves, solver)
        specified_spd = 0 if self.leg2.float_spread is None else self.leg2.float_spread
        curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        return self.leg2._spread(-irs_npv, curves[2], curves[3]) + specified_spd
        # leg2_analytic_delta = self.leg2.analytic_delta(curves[2], curves[3])
        # return irs_npv / leg2_analytic_delta + specified_spd


class Swap(IRS):
    """
    Alias for :class:`~rateslib.instruments.IRS`.
    """


class SBS(BaseDerivative):
    """
    Create a single currency basis swap composing two
    :class:`~rateslib.legs.FloatLeg` s.

    Parameters
    ----------
    args : tuple
        Required positional args to :class:`BaseDerivative`.
    float_spread : float, optional
        The spread applied to the :class:`~rateslib.legs.FloatLeg`. Can be set to
        `None` and designated
        later, perhaps after a mid-market spread for all periods has been calculated.
    spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    fixings : float, list, or Series optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given, is used as the first *m* RFR
        fixings for that :class:`~rateslib.periods.FloatPeriod`. If a datetime
        indexed ``Series`` will use the fixings that are available in that object,
        and derive the rest from the ``curve``.
    fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    leg2_float_spread : float or None
        The floating spread applied in a simple way (after daily compounding) to the
        second :class:`~rateslib.legs.FloatLeg`. If `None` will be set to zero.
        float_spread : float, optional
        The spread applied to the :class:`~rateslib.legs.FloatLeg`. Can be set to
        `None` and designated
        later, perhaps after a mid-market spread for all periods has been calculated.
    leg2_spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    leg2_fixings : float, list, or Series optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given, is used as the first *m* RFR
        fixings for that :class:`~rateslib.periods.FloatPeriod`. If a datetime
        indexed ``Series`` will use the fixings that are available in that object,
        and derive the rest from the ``curve``.
    leg2_fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    leg2_method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.
    """
    _float_spread_mixin = True
    _leg2_float_spread_mixin = True

    def __init__(
        self,
        *args,
        float_spread: Optional[float] = None,
        spread_compound_method: Optional[str] = None,
        fixings: Optional[Union[float, list, Series]] = None,
        fixing_method: Optional[str] = None,
        method_param: Optional[int] = None,
        leg2_float_spread: Optional[float] = None,
        leg2_spread_compound_method: Optional[str] = None,
        leg2_fixings: Optional[Union[float, list, Series]] = None,
        leg2_fixing_method: Optional[str] = None,
        leg2_method_param: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._float_spread = float_spread
        self._leg2_float_spread = leg2_float_spread
        self.leg1 = FloatLeg(
            float_spread=float_spread,
            spread_compound_method=spread_compound_method,
            fixings=fixings,
            fixing_method=fixing_method,
            method_param=method_param,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FloatLeg(
            float_spread=leg2_float_spread,
            spread_compound_method=leg2_spread_compound_method,
            fixings=leg2_fixings,
            fixing_method=leg2_fixing_method,
            method_param=leg2_method_param,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            notional=self.leg2_notional,
            currency=self.leg2_currency,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )

    def analytic_delta(self, *args, **kwargs):
        """
        Return the analytic delta of a leg of the derivative object.

        See :meth:`BaseDerivative.analytic_delta`.

        Examples
        --------
        .. ipython:: python

           forecasting_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.98})
           forecasting_curve2 = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.97})
           discounting_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.985})

        .. ipython:: python

           sbs = SBS(
               effective=dt(2022, 2, 15),
               termination=dt(2022, 8, 15),
               frequency="Q",
               leg2_frequency="S",
               leg2_float_spread=-50.0,
               convention="Act360",
               payment_lag=2,
               notional=1e9,
           )
           sbs.analytic_delta(forecasting_curve, discounting_curve, leg=1)
        """
        return super().analytic_delta(*args, **kwargs)

    def cashflows(
        self,
        curves: Optional[Union[Curve, str, list]]= None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the properties of all legs used in calculating cashflows.

        See :meth:`BaseDerivative.cashflows`.

        Examples
        --------
        .. ipython:: python

           sbs.cashflows([forecasting_curve, discounting_curve, forecasting_curve2])
        """
        return super().cashflows(curves, solver, fx, base)

    def npv(
        self,
        curves: Optional[Union[Curve, str, list]]= None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the NPV of the derivative object by summing legs.

        See :meth:`BaseDerivative.npv`.

        Examples
        --------
        .. ipython:: python

           sbs.npv([forecasting_curve, discounting_curve, forecasting_curve2])
        """
        return super().npv(curves, solver, fx, base)

    def rate(
        self,
        curves: Optional[Union[Curve, str, list]]= None,
        solver: Optional[Solver] = None,
        leg: int = 1,
    ):
        """
        Return the mid-market float spread on the specified leg of the SBS.

        Parameters
        ----------
        curves : Curve, str or list of such
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for floating leg1.
            - Discounting :class:`~rateslib.curves.Curve` for both legs.
            - Forecasting :class:`~rateslib.curves.Curve` for floating leg2.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that constructs
            :class:`~rateslib.curves.Curve` from calibrating
            instruments.
        leg: int in [1, 2]
            Specify which leg the spread calculation is applied to.

        Returns
        -------
        float, Dual or Dual2

        Examples
        --------
        .. ipython:: python

           sbs.rate([forecasting_curve, discounting_curve, forecasting_curve2], leg=1)
           sbs.rate([forecasting_curve, discounting_curve, forecasting_curve2], leg=2)
        """
        irs_npv = self.npv(curves, solver)
        curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        if leg == 1:
            leg_obj, args = self.leg1, (curves[0], curves[1])
        else:
            leg_obj, args = self.leg2, (curves[2], curves[3])

        specified_spd = 0 if leg_obj.float_spread is None else leg_obj.float_spread
        return leg_obj._spread(-irs_npv, *args) + specified_spd

        # irs_npv = self.npv(curves, solver)
        # curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        # if leg == 1:
        #     args = (curves[0], curves[1])
        # else:
        #     args = (curves[2], curves[3])
        # leg_analytic_delta = getattr(self, f"leg{leg}").analytic_delta(*args)
        # adjust = getattr(self, f"leg{leg}").float_spread
        # adjust = 0 if adjust is None else adjust
        # _ = irs_npv / leg_analytic_delta + adjust
        # return _

    def spread(self, *args, **kwargs):
        """
        Return the mid-market float spread on the specified leg of the SBS.

        Alias for :meth:`~rateslib.instruments.SBS.rate`.
        """
        return self.rate(*args, **kwargs)


class FRA(Sensitivities, _AttributesMixin):
    """
    Create a forward rate agreement composing a :class:`~rateslib.periods.FixedPeriod`
    and :class:`~rateslib.periods.FloatPeriod` valued in a customised manner.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fixed_rate : float or None
        The fixed rate applied to the :class:`~rateslib.legs.FixedLeg`. If `None`
        will be set to mid-market when curves are provided.
    float_spread : float, optional
        The spread applied to the :class:`~rateslib.legs.FloatLeg`. Can be set to
        `None` and designated
        later, perhaps after a mid-market spread for all periods has been calculated.
    spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.

    Notes
    -----
    FRAs are a legacy derivative whose ``fixing_method`` is set to *"ibor"*.

    ``effective`` and ``termination`` are not adjusted prior to initialising
    ``Periods``. Care should be taken to enter these exactly.
    """
    _fixed_rate_mixin = True

    def __init__(
        self,
        effective: datetime,
        termination: Union[datetime, str],
        frequency: str,
        modifier: Optional[Union[str, bool]] = False,
        calendar: Optional[Union[CustomBusinessDay, str]] = None,
        notional: Optional[float] = None,
        convention: Optional[str] = None,
        method_param: Optional[int] = None,
        payment_lag: Optional[int] = None,
        fixed_rate: Optional[float] = None,
        fixings: Optional[Union[float, Series]] = None,
        currency: Optional[str] = None,
        curves: Optional[Union[str, list, Curve]] = None,
    ):
        self.curves = curves
        self.currency = defaults.base_currency if currency is None else currency.lower()

        if isinstance(modifier, bool):  # then get default
            modifier_: Optional[str] = defaults.modifier
        else:
            modifier_ = modifier.upper()
        self.modifier = modifier_

        if payment_lag is None:
            self.payment_lag = defaults.payment_lag_specific["FRA"]
        else:
            self.payment_lag = payment_lag
        self.calendar = get_calendar(calendar)
        self.payment = add_tenor(effective, f"{self.payment_lag}B", None, self.calendar)

        if isinstance(termination, str):
            # if termination is string the end date is calculated as unadjusted
            termination = add_tenor(
                effective, termination, self.modifier, self.calendar
            )

        self.notional = defaults.notional if notional is None else notional

        convention = defaults.convention if convention is None else convention

        self._fixed_rate = fixed_rate
        self.leg1 = FixedPeriod(
            start=effective,
            end=termination,
            payment=self.payment,
            convention=convention,
            frequency=frequency,
            stub=False,
            currency=self.currency,
            fixed_rate=fixed_rate,
            notional=notional,
        )

        self.leg2 = FloatPeriod(
            start=effective,
            end=termination,
            payment=termination,
            spread_compound_method="none_simple",
            fixing_method="ibor",
            method_param=method_param,
            fixings=fixings,
            convention=convention,
            frequency=frequency,
            stub=False,
            currency=self.currency,
            notional=self.notional,
        )  # FloatPeriod is used only to access the rate method for calculations.

    def analytic_delta(
        self,
        curve: Curve,
        disc_curve: Optional[Curve] = None,
        fx: Union[float, FXRates, FXForwards] = 1.0,
        base: Optional[str] = None,
    ):
        """
        Return the analytic delta of the FRA.

        Parameters
        ----------
        curve : Curve
            The forecasting curve object.
        disc_curve : Curve, optional
            The discounting curve object. Set equal to ``curve`` if not given.
        fx : float, FXRates, FXForwards, optional
            The immediate settlement FX rate that will be used to convert values
            into another currency. A given `float` is used directly. If giving a
            ``FXRates`` or ``FXForwards`` object, converts from local currency
            into ``base``.
        base : str, optional
            The base currency to convert cashflows into (3 digit code), set by default.
            Only used if ``fx_rate`` is an ``FXRates`` or ``FXForwards`` object.

        Returns
        -------
        flaat, Dual or Dual2

        Examples
        --------
        .. ipython:: python

           forecasting_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.98})
           discounting_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.985})

        .. ipython:: python

           fra = FRA(
               effective=dt(2022, 3, 15),
               termination=dt(2022, 6, 15),
               frequency="Q",
               convention="Act360",
               fixed_rate=2.50,
               notional=1000000,
               currency="gbp"
           )
           fra.analytic_delta(forecasting_curve, discounting_curve)
        """
        disc_curve = disc_curve or curve
        fx, base = _get_fx_and_base(self.currency, fx, base)
        rate = self.rate([curve])
        _ = self.notional * self.leg1.dcf * disc_curve[self.payment] / 10000
        return fx * _ / (1 + self.leg1.dcf * rate / 100)

    def npv(
        self,
        curves: Optional[Union[str, list, Curve]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the NPV of the derivative.

        See :meth:`BaseDerivative.npv`.

        Examples
        --------
        .. ipython:: python

           fra.npv([forecasting_curve, discounting_curve])

        .. ipython:: python

           fxr = FXRates({"gbpusd": 2.0})
           fra.npv([forecasting_curve, discounting_curve], None, fxr, "usd")
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        fx, base = _get_fx_and_base(self.currency, fx, base)
        return fx * self.cashflow(curves[0]) * curves[1][self.payment]

    def rate(
        self,
        curves: Optional[Union[str, list, Curve]] = None,
        solver: Optional[Solver] = None,
    ):
        """
        Return the mid-market rate of the FRA.

        Only the forecasting curve is required to price an FRA.

        Parameters
        ----------
        curves : Curve, str or list of such
            A single :class:`~rateslib.curves.Curve` or id or a list of such.
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for floating leg.
            - Discounting :class:`~rateslib.curves.Curve` for floating leg.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that
            constructs :class:`~rateslib.curves.Curve` from calibrating instruments.

        Returns
        -------
        float, Dual or Dual2

        Examples
        --------
        .. ipython:: python

           fra.rate(forecasting_curve)
        """
        curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        return self.leg2.rate(curves[0])

    def cashflow(self, curve: Union[Curve, LineCurve]):
        """
        Calculate the local currency cashflow on the FRA from current floating rate
        and fixed rate.

        Parameters
        ----------
        curve : Curve or LineCurve,
            The forecasting curve for determining the floating rate.

        Returns
        -------
        float, Dual or Dual2

        Examples
        --------
        .. ipython:: python

           fra.cashflow(forecasting_curve)
        """
        if self.fixed_rate is None:
            return 0  # set the fixed rate = to floating rate netting to zero
        rate = self.leg2.rate(curve)
        cf = self.notional * self.leg1.dcf * (rate - self.fixed_rate) / 100
        cf /= (1 + self.leg1.dcf * rate / 100)
        return cf

    def cashflows(
        self,
        curves: Optional[Union[str, list, Curve]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[Union[float, FXRates, FXForwards]] = None,
        base: Optional[str] = None,
    ):
        """
        Return the properties of the leg used in calculating cashflows.

        Parameters
        ----------
        args :
            Positional arguments supplied to :meth:`~rateslib.periods.BasePeriod.cashflows`.
        kwargs :
            Keyword arguments supplied to :meth:`~rateslib.periods.BasePeriod.cashflows`.

        Returns
        -------
        DataFrame

        Examples
        --------
        .. ipython:: python

           fxr = FXRates({"gbpusd": 2.0})
           fra.cashflows([forecasting_curve, discounting_curve], None, fxr, "usd")
        """
        curves, _ = self._get_curves_and_fx_maybe_from_solver(solver, curves, None)
        fx, base = _get_fx_and_base(self.currency, fx, base)
        cf = float(self.cashflow(curves[0]))
        npv_local = self.cashflow(curves[0]) * curves[1][self.payment]

        _spread = None if self.fixed_rate is None else -float(self.fixed_rate) * 100
        cfs = self.leg1.cashflows(curves[0], curves[1], fx, base)
        cfs[defaults.headers["type"]] = "FRA"
        cfs[defaults.headers["payment"]] = self.payment
        cfs[defaults.headers["cashflow"]] = cf
        cfs[defaults.headers["rate"]] = float(self.rate(curves[1]))
        cfs[defaults.headers["spread"]] = _spread
        cfs[defaults.headers["npv"]] = npv_local
        cfs[defaults.headers["fx"]] = float(fx)
        cfs[defaults.headers["npv_fx"]] = npv_local * float(fx)
        return DataFrame.from_records([cfs])


### Multi-currency derivatives


class BaseXCS(BaseDerivative):
    """
    Base class with common methods for multi-currency ``Derivatives``.
    """
    _is_mtm = False

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        # TODO set payment_lag_exchange and leg2.. in init here, including inherit and default lookup.
        return super().__init__(*args, **kwargs)

    @property
    def fx_fixings(self):
        return self._fx_fixings

    @fx_fixings.setter
    def fx_fixings(self, value):
        self._fx_fixings = value
        self._set_leg2_notional(value)

    def _initialise_fx_fixings(self, fx_fixings):
        """
        Sets the `fx_fixing` for non-mtm XCS instruments, which require only a single
        value.
        """
        if not self._is_mtm:
            self.pair = self.leg1.currency + self.leg2.currency
            # if self.fx_fixing is None this indicates the swap is unfixed and will be set
            # later. If a fixing is given this means the notional is fixed without any
            # further sensitivity, hence the downcast to a float below.
            if isinstance(fx_fixings, FXForwards):
                self.fx_fixings = float(
                    fx_fixings.rate(self.pair, self.leg2.periods[0].payment))
            elif isinstance(fx_fixings, FXRates):
                self.fx_fixings = float(fx_fixings.rate(self.pair))
            elif isinstance(fx_fixings, (float, Dual, Dual2)):
                self.fx_fixings = float(fx_fixings)
            else:
                self._fx_fixings = None

    def _set_fx_fixings(self, fx):
        """
        Checks the `fx_fixings` and sets them according to given object if null.

        Used by ``rate`` and ``npv`` methods when ``fx_fixings`` are not
        initialised but required for pricing and can be inferred from an FX object.
        """
        if not self._is_mtm:  # then we manage the initial FX from the pricing object.
            if self.fx_fixings is None:
                if fx is None:
                    if defaults.no_fx_fixings_for_xcs.lower() == "raise":
                        raise ValueError(
                            "`fx` is required when `fx_fixing` is not pre-set and "
                            "if rateslib option `no_fx_fixings_for_xcs` is set to "
                            "'raise'."
                        )
                    else:
                        fx_fixing = 1.0
                        if defaults.no_fx_fixings_for_xcs.lower() == "warn":
                            warnings.warn(
                                "Using 1.0 for FX, no `fx` or `fx_fixing` given and "
                                "rateslib option `no_fx_fixings_for_xcs` is set to "
                                "'warn'.",
                                UserWarning
                            )
                else:
                    fx_fixing = fx.rate(
                        self.pair,
                        self.leg2.periods[0].payment
                    )
                self._set_leg2_notional(fx_fixing)
        else:
            self._set_leg2_notional(fx)

    def _set_leg2_notional(self, fx_arg: Union[float, FXForwards]):
        """
        Update the notional on leg2 (foreign leg) if the initial fx rate is unfixed.

        ----------
        fx_arg : float or FXForwards
            For non-MTM XCSs this input must be a float.
            The FX rate to use as the initial notional fixing.
            Will only update the leg if ``NonMtmXCS.fx_fixings`` has been initially
            set to `None`.

            For MTM XCSs this input must be ``FXForwards``.
            The FX object from which to determine FX rates used as the initial
            notional fixing, and to determine MTM cashflow exchanges.
        """
        if self._is_mtm:
            self.leg2._set_periods(fx_arg)
            self.leg2_notional = self.leg2.notional
        else:
            self.leg2_notional = self.leg1.notional * -fx_arg
            self.leg2.notional = self.leg2_notional
            self.leg2_amortization = self.leg1.amortization * -fx_arg
            self.leg2.amortization = self.leg2_amortization

    def npv(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[FXForwards] = None,
        base: Optional[str] = None,
    ):
        """
        Return the NPV of the derivative by summing legs.

        .. warning::

           If ``fx_fixing`` has not been set for the instrument requires
           ``fx`` as an FXForwards object to dynamically determine this.

        See :meth:`BaseDerivative.npv`.
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        base = self.leg1.currency if base is None else base
        self._set_fx_fixings(fx)
        if self._is_mtm:
            self.leg2._do_not_repeat_set_periods = True

        if "Fixed" in type(self.leg1).__name__ and self.fixed_rate is None:
            mid_market_rate = self.rate(curves, solver, fx, leg=1)
            self.leg1.fixed_rate = mid_market_rate
        if "Fixed" in type(self.leg2).__name__ and self.leg2_fixed_rate is None:
            if type(self).__name__ == "FXSwap":
                mid_market_rate = self.rate(curves, solver, fx, fixed_rate=True)
            else:
                mid_market_rate = self.rate(curves, solver, fx, leg=2)
            self.leg2.fixed_rate = mid_market_rate

        ret = super().npv(curves, solver, fx, base)
        if self._is_mtm:
            self.leg2._do_not_repeat_set_periods = False  # reset for next calculation
        return ret

    def rate(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[FXForwards] = None,
        leg: int = 1,
    ):
        """
        Return the mid-market pricing parameter of the XCS.

        Parameters
        ----------
        curves : list of Curves
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for leg1 (if floating).
            - Discounting :class:`~rateslib.curves.Curve` for leg1.
            - Forecasting :class:`~rateslib.curves.Curve` for leg2 (if floating).
            - Discounting :class:`~rateslib.curves.Curve` for leg2.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that
            constructs :class:`~rateslib.curves.Curve` from calibrating instruments.
        fx : FXForwards, optional
            The FX forwards object that is used to determine the initial FX fixing for
            determining ``leg2_notional``, if not specified at initialisation, and for
            determining mark-to-market exchanges on mtm XCSs.
        leg : int in [1, 2]
            The leg whose pricing parameter is to be determined.

        Returns
        -------
        float, Dual or Dual2

        Notes
        -----
        Fixed legs have pricing parameter returned in percentage terms, and
        float legs have pricing parameter returned in basis point (bp) terms.

        If the ``XCS`` type is specified without a ``fixed_rate`` on any leg then an
        implied ``float_spread`` will return as its originaly value or zero since
        the fixed rate used
        for calculation is the implied mid-market rate including the
        current ``float_spread`` parameter.

        Examples
        --------
        """
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)

        if leg == 1:
            tgt_fore_curve, tgt_disc_curve = curves[0], curves[1]
            alt_fore_curve, alt_disc_curve = curves[2], curves[3]
        else:
            tgt_fore_curve, tgt_disc_curve = curves[2], curves[3]
            alt_fore_curve, alt_disc_curve = curves[0], curves[1]

        leg2 = 1 if leg == 2 else 2
        tgt_str, alt_str = "" if leg == 1 else "leg2_", "" if leg2 == 1 else "leg2_"
        tgt_leg, alt_leg = getattr(self, f"leg{leg}"),  getattr(self, f"leg{leg2}")
        base = tgt_leg.currency

        _is_float_tgt_leg = "Float" in type(tgt_leg).__name__
        _is_float_alt_leg = "Float" in type(alt_leg).__name__
        if not _is_float_alt_leg and getattr(self, f"{alt_str}fixed_rate") is None:
            raise ValueError(
                "Cannot solve for a `fixed_rate` or `float_spread` where the "
                "`fixed_rate` on the non-solvable leg is None."
            )

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.

        if tgt_leg._is_linear:

            if not _is_float_tgt_leg and getattr(self, f"{tgt_str}fixed_rate") is None:
                # set the target fixed leg to a null fixed rate for calculation
                tgt_leg.fixed_rate = 0.

            self._set_fx_fixings(fx)
            if self._is_mtm:
                self.leg2._do_not_repeat_set_periods = True

            tgt_leg_npv = tgt_leg.npv(tgt_fore_curve, tgt_disc_curve, fx, base)
            alt_leg_npv = alt_leg.npv(alt_fore_curve, alt_disc_curve, fx, base)
            fx_a_delta = 1.0 if not tgt_leg._is_mtm else fx
            _ = tgt_leg._spread(
                -(tgt_leg_npv+alt_leg_npv), tgt_fore_curve, tgt_disc_curve, fx_a_delta
            )

            specified_spd = 0.
            if _is_float_tgt_leg and \
                    not(getattr(self, f"{tgt_str}float_spread") is None):
                specified_spd = tgt_leg.float_spread
            elif not _is_float_tgt_leg:
                specified_spd = tgt_leg.fixed_rate * 100

            _ += specified_spd

            if self._is_mtm:
                self.leg2._do_not_repeat_set_periods = False  # reset the mtm calc

        else:
            # need to set_order(2) for XCS.
            # npv = self.npv(curves, solver, fx, leg_.currency)
            raise NotImplementedError("Dual and Dual2 upcasting not complete.")
            # the problem here is that spread requires Dual2 but fx_fixing
            # calculates a Dual by default. Needs a manual overwrite.
            # set_order(1) for XCS

        return _ if _is_float_tgt_leg else _ * 0.01

    def spread(self, *args, **kwargs):
        """
        Alias for :meth:`~rateslib.instruments.BaseXCS.rate`
        """
        return self.rate(*args, **kwargs)

    def cashflows(
        self,
        curves: Optional[Union[Curve, str, list]] = None,
        solver: Optional[Solver] = None,
        fx: Optional[FXForwards] = None,
        base: Optional[str] = None,
    ):
        curves, fx = self._get_curves_and_fx_maybe_from_solver(solver, curves, fx)
        self._set_fx_fixings(fx)
        if self._is_mtm:
            self.leg2._do_not_repeat_set_periods = True

        ret = super().cashflows(curves, solver, fx, base)
        if self._is_mtm:
            self.leg2._do_not_repeat_set_periods = False  # reset the mtm calc
        return ret


class NonMtmXCS(BaseXCS):
    """
    Create a non-mark-to-market cross currency swap (XCS) derivative composing two
    :class:`~rateslib.legs.FloatLegExchange` s.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fx_fixing : float, FXForwards or None
        The initial FX fixing where leg 1 is considered the domestic currency. For
        example for an ESTR/SOFR XCS in 100mm EUR notional a value of 1.10 for
        `fx_fixing` implies the notional on leg 2 is 110m USD. If `None` determines
        this dynamically later.
    float_spread : float or None
        The float spread applied in a simple way (after daily compounding) to leg 2.
        If `None` will be set to zero.
    spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    leg2_float_spread : float or None
        The float spread applied in a simple way (after daily compounding) to leg 2.
        If `None` will be set to zero.
    leg2_spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    leg2_fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    leg2_fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    leg2_method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    leg2_payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.

    Notes
    -----
    Non-mtm cross currency swaps create identical yet opposite currency exchanges at
    the effective date and the payment termination date of the swap. There are no
    intermediate currency exchanges.

    .. note::

       Although non-MTM XCSs have an ``fx_fixing`` argument, which consists of a single,
       initial FX fixing, this is internally mapped to the ``fx_fixings`` attribute,
       which, for MTM XCSs, provides all the FX fixings throughout the swap.

    """
    _float_spread_mixin = True
    _leg2_float_spread_mixin = True

    def __init__(
        self,
        *args,
        fx_fixing: Optional[Union[float, FXRates, FXForwards]] = None,
        float_spread: Optional[float] = None,
        fixings: Optional[Union[float, list]] = None,
        fixing_method: Optional[str] = None,
        method_param: Optional[int] = None,
        spread_compound_method: Optional[str] = None,
        payment_lag_exchange: Optional[int] = None,
        leg2_float_spread: Optional[float] = None,
        leg2_fixings: Optional[Union[float, list]] = None,
        leg2_fixing_method: Optional[str] = None,
        leg2_method_param: Optional[int] = None,
        leg2_spread_compound_method: Optional[str] = None,
        leg2_payment_lag_exchange: Optional[int] = "inherit",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        self._leg2_float_spread = leg2_float_spread
        self._float_spread = float_spread
        self.leg1 = FloatLegExchange(
            float_spread=float_spread,
            fixings=fixings,
            fixing_method=fixing_method,
            method_param=method_param,
            spread_compound_method=spread_compound_method,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FloatLegExchange(
            float_spread=leg2_float_spread,
            fixings=leg2_fixings,
            fixing_method=leg2_fixing_method,
            method_param=leg2_method_param,
            spread_compound_method=leg2_spread_compound_method,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            notional=self.leg2_notional,
            currency=self.leg2_currency,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )
        self._initialise_fx_fixings(fx_fixing)

    def _rate2(
        self,
        curve_domestic: Curve,
        disc_curve_domestic: Curve,
        curve_foreign: Curve,
        disc_curve_foreign: Curve,
        fx_rate: Union[float, Dual],
        fx_settlement: Optional[datetime] = None,
    ):  # pragma: no cover
        """
        Determine the mid-market floating spread on domestic leg 1, to equate leg 2.

        Parameters
        ----------
        curve_domestic : Curve
            The forecast :class:`Curve` for domestic currency cashflows.
        disc_curve_domestic : Curve
            The discount :class:`Curve` for domestic currency cashflows.
        curve_foreign : Curve
            The forecasting :class:`Curve` for foreign currency cashflows.
        disc_curve_foreign : Curve
            The discounting :class:`Curve` for foreign currency cashflows.
        fx_rate : float, optional
            The FX rate for valuing cashflows.
        fx_settlement : datetime, optional
            The date for settlement of ``fx_rate``. If spot then should be input as T+2.
            If `None`, is assumed to be immediate settlement.

        Returns
        -------
        BP Spread to leg 1 : Dual
        """
        npv = self.npv(
            curve_domestic,
            disc_curve_domestic,
            curve_foreign,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        f_0 = forward_fx(
            disc_curve_domestic.node_dates[0],
            disc_curve_domestic,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        leg1_analytic_delta = f_0 * self.leg1.analytic_delta(curve_domestic, disc_curve_domestic)
        spread = npv / leg1_analytic_delta
        return spread

    def _npv2(
        self,
        curve_domestic: Curve,
        disc_curve_domestic: Curve,
        curve_foreign: Curve,
        disc_curve_foreign: Curve,
        fx_rate: Union[float, Dual],
        fx_settlement: Optional[datetime] = None,
        base: str = None
    ):  # pragma: no cover
        """
        Return the NPV of the non-mtm XCS.

        Parameters
        ----------
        curve_domestic : Curve
            The forecast :class:`Curve` for domestic currency cashflows.
        disc_curve_domestic : Curve
            The discount :class:`Curve` for domestic currency cashflows.
        curve_foreign : Curve
            The forecasting :class:`Curve` for foreign currency cashflows.
        disc_curve_foreign : Curve
            The discounting :class:`Curve` for foreign currency cashflows.
        fx_rate : float, optional
            The FX rate for valuing cashflows.
        fx_settlement : datetime, optional
            The date for settlement of ``fx_rate``. If spot then should be input as T+2.
            If `None`, is assumed to be immediate settlement.
        base : str, optional
            The base currency to express the NPV, either `"domestic"` or `"foreign"`.
            Set by default.
        """
        base = defaults.fx_swap_base if base is None else base
        f_0 = forward_fx(
            disc_curve_domestic.node_dates[0],
            disc_curve_domestic,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        fx = forward_fx(
            self.effective,
            disc_curve_domestic,
            disc_curve_foreign,
            f_0,
        )
        self._set_leg2_notional(fx)
        leg1_npv = self.leg1.npv(curve_domestic)
        leg2_npv = self.leg2.npv(curve_foreign)

        if base == "foreign":
            return leg1_npv * f_0 + leg2_npv
        elif base == "domestic":
            return leg1_npv + leg2_npv / f_0
        else:
            raise ValueError('`base` should be either "domestic" or "foreign".')

    def _cashflows2(
        self,
        curve_domestic: Optional[Curve] = None,
        disc_curve_domestic: Optional[Curve] = None,
        curve_foreign: Optional[Curve] = None,
        disc_curve_foreign: Optional[Curve] = None,
        fx_rate: Optional[Union[float, Dual]] = None,
        fx_settlement: Optional[datetime] = None,
        base: Optional[str] = None,
    ):  # pragma: no cover
        """
        Return the properties of all legs used in calculating cashflows.

        Parameters
        ----------
        curve_domestic : Curve
            The forecast :class:`Curve` for domestic currency cashflows.
        disc_curve_domestic : Curve
            The discount :class:`Curve` for domestic currency cashflows.
        curve_foreign : Curve
            The forecasting :class:`Curve` for foreign currency cashflows.
        disc_curve_foreign : Curve
            The discounting :class:`Curve` for foreign currency cashflows.
        fx_rate : float, optional
            The FX rate for valuing cashflows.
        fx_settlement : datetime, optional
            The date for settlement of ``fx_rate``. If spot then should be input as T+2.
            If `None`, is assumed to be immediate settlement.
        base : str, optional
            The base currency to express the NPV, either `"domestic"` or `"foreign"`.
            Set by default.

        Returns
        -------
        DataFrame
        """
        f_0 = forward_fx(
            disc_curve_domestic.node_dates[0],
            disc_curve_domestic,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        base = defaults.fx_swap_base if base is None else base
        if base == "foreign":
            d_fx, f_fx = f_0, 1.0
        elif base == "domestic":
            d_fx, f_fx = 1.0,  1.0 / f_0
        else:
            raise ValueError('`base` should be either "domestic" or "foreign".')
        self._set_leg2_notional(f_0)
        return concat([
            self.leg1.cashflows(curve_domestic, disc_curve_domestic, d_fx),
            self.leg2.cashflows(curve_foreign, disc_curve_foreign, f_fx),
        ], keys=["leg1", "leg2"],
        )


class NonMtmFixedFloatXCS(BaseXCS):
    """
    Create a non-mark-to-market cross currency swap (XCS) derivative composing a
    :class:`~rateslib.legs.FixedLegExchange` and a
    :class:`~rateslib.legs.FloatLegExchange`.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fx_fixing : float, FXForwards or None
        The initial FX fixing where leg 1 is considered the domestic currency. For
        example for an ESTR/SOFR XCS in 100mm EUR notional a value of 1.10 for `fx0`
        implies the notional on leg 2 is 110m USD. If `None` determines this
        dynamically.
    fixed_rate : float or None
        The fixed rate applied to leg 1.
        If `None` will be set to mid-market when curves are provided.
    payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    leg2_float_spread2 : float or None
        The float spread applied in a simple way (after daily compounding) to leg 2.
        If `None` will be set to zero.
    leg2_spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    leg2_fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    leg2_fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    leg2_method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    leg2_payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.

    Notes
    -----
    Non-mtm cross currency swaps create identical yet opposite currency exchanges at
    the effective date and the payment termination date of the swap. There are no
    intermediate currency exchanges.
    """
    _fixed_rate_mixin = True
    _leg2_float_spread_mixin = True

    def __init__(
        self,
        *args,
        fx_fixing: Optional[Union[float, FXRates, FXForwards]] = None,
        fixed_rate: Optional[float] = None,
        payment_lag_exchange: Optional[int] = None,
        leg2_float_spread: Optional[float] = None,
        leg2_fixings: Optional[Union[float, list]] = None,
        leg2_fixing_method: Optional[str] = None,
        leg2_method_param: Optional[int] = None,
        leg2_spread_compound_method: Optional[str] = None,
        leg2_payment_lag_exchange: Optional[int] = "inherit",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        self._leg2_float_spread = leg2_float_spread
        self._fixed_rate = fixed_rate
        self.leg1 = FixedLegExchange(
            fixed_rate=fixed_rate,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FloatLegExchange(
            float_spread=leg2_float_spread,
            fixings=leg2_fixings,
            fixing_method=leg2_fixing_method,
            method_param=leg2_method_param,
            spread_compound_method=leg2_spread_compound_method,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            notional=self.leg2_notional,
            currency=self.leg2_currency,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )
        self._initialise_fx_fixings(fx_fixing)


class NonMtmFixedFixedXCS(BaseXCS):
    """
    Create a non-mark-to-market cross currency swap (XCS) derivative composing two
    :class:`~rateslib.legs.FixedLegExchange` s.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fx_fixing : float, FXForwards or None
        The initial FX fixing where leg 1 is considered the domestic currency. For
        example for an ESTR/SOFR XCS in 100mm EUR notional a value of 1.10 for `fx0`
        implies the notional on leg 2 is 110m USD. If `None` determines this
        dynamically.
    fixed_rate : float or None
        The fixed rate applied to leg 1.
        If `None` will be set to mid-market when curves are provided.
    payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    leg2_fixed_rate : float or None
        The fixed rate applied to leg 2.
        If `None` will be set to mid-market when curves are provided.
        Must set the ``fixed_rate`` on at least one leg.
    leg2_payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.

    Notes
    -----
    Non-mtm cross currency swaps create identical yet opposite currency exchanges at
    the effective date and the payment termination date of the swap. There are no
    intermediate currency exchanges.
    """
    _fixed_rate_mixin = True
    _leg2_fixed_rate_mixin = True

    def __init__(
        self,
        *args,
        fx_fixing: Optional[Union[float, FXRates, FXForwards]] = None,
        fixed_rate: Optional[float] = None,
        payment_lag_exchange: Optional[int] = None,
        leg2_fixed_rate: Optional[float] = None,
        leg2_payment_lag_exchange: Optional[int] = "inherit",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        self._leg2_fixed_rate = leg2_fixed_rate
        self._fixed_rate = fixed_rate
        self.leg1 = FixedLegExchange(
            fixed_rate=fixed_rate,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FixedLegExchange(
            fixed_rate=leg2_fixed_rate,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            notional=self.leg2_notional,
            currency=self.leg2_currency,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )
        self._initialise_fx_fixings(fx_fixing)

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.


class XCS(BaseXCS):
    """
    Create a mark-to-market cross currency swap (XCS) derivative instrument.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fx_fixings : float, Dual, Dual2, list of such
        Specify a known initial FX fixing or a list of such for historical legs,
        where leg 1 is considered the domestic currency. For
        example for an ESTR/SOFR XCS in 100mm EUR notional a value of 1.10 for
        `fx_fixings` implies the notional on leg 2 is 110m USD.
        Fixings that are not specified will be calculated at pricing time with an
        :class:`~rateslib.fx.FXForwards` object.
    float_spread : float or None
        The float spread applied in a simple way (after daily compounding) to leg 2.
        If `None` will be set to zero.
    spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    leg2_float_spread : float or None
        The float spread applied in a simple way (after daily compounding) to leg 2.
        If `None` will be set to zero.
    leg2_spread_compound_method : str, optional
        The method to use for adding a floating spread to compounded rates. Available
        options are `{"none_simple", "isda_compounding", "isda_flat_compounding"}`.
    leg2_fixings : float or list, optional
        If a float scalar, will be applied as the determined fixing for the first
        period. If a list of *n* fixings will be used as the fixings for the first *n*
        periods. If any sublist of length *m* is given as the first *m* RFR fixings
        within individual curve and composed into the overall rate.
    leg2_fixing_method : str, optional
        The method by which floating rates are determined, set by default. See notes.
    leg2_method_param : int, optional
        A parameter that is used for the various ``fixing_method`` s. See notes.
    leg2_payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.

    Notes
    -----
    Mtm cross currency swaps create notional exchanges on the foreign leg throughout
    the life of the derivative and adjust the notional on which interest is accrued.

    .. warning::

       ``Amortization`` is not used as an argument by ``XCS``.
    """
    _float_spread_mixin = True
    _leg2_float_spread_mixin = True
    _is_mtm = True

    def __init__(
        self,
        *args,
        fx_fixings: Union[list, float, Dual, Dual2] = [],
        float_spread: Optional[float] = None,
        fixings: Optional[Union[float, list]] = None,
        fixing_method: Optional[str] = None,
        method_param: Optional[int] = None,
        spread_compound_method: Optional[str] = None,
        payment_lag_exchange: Optional[int] = None,
        leg2_float_spread: Optional[float] = None,
        leg2_fixings: Optional[Union[float, list]] = None,
        leg2_fixing_method: Optional[str] = None,
        leg2_method_param: Optional[int] = None,
        leg2_spread_compound_method: Optional[str] = None,
        leg2_payment_lag_exchange: Optional[int] = "inherit",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        if fx_fixings is None:
            raise ValueError(
                "`fx_fixings` for MTM XCS should be entered as an empty list, not None."
            )
        self._fx_fixings = fx_fixings
        self._leg2_float_spread = leg2_float_spread
        self._float_spread = float_spread
        self.leg1 = FloatLegExchange(
            float_spread=float_spread,
            fixings=fixings,
            fixing_method=fixing_method,
            method_param=method_param,
            spread_compound_method=spread_compound_method,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FloatLegExchangeMtm(
            float_spread=leg2_float_spread,
            fixings=leg2_fixings,
            fixing_method=leg2_fixing_method,
            method_param=leg2_method_param,
            spread_compound_method=leg2_spread_compound_method,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            currency=self.leg2_currency,
            alt_currency=self.currency,
            alt_notional=-self.notional,
            fx_fixings=fx_fixings,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )

    def _npv2(
        self,
        curve_domestic: Curve,
        disc_curve_domestic: Curve,
        curve_foreign: Curve,
        disc_curve_foreign: Curve,
        fx_rate: Union[float, Dual],
        fx_settlement: Optional[datetime] = None,
        base: str = None
    ):  # pragma: no cover
        """
        Return the NPV of the non-mtm XCS.

        Parameters
        ----------
        curve_domestic : Curve
            The forecast :class:`Curve` for domestic currency cashflows.
        disc_curve_domestic : Curve
            The discount :class:`Curve` for domestic currency cashflows.
        curve_foreign : Curve
            The forecasting :class:`Curve` for foreign currency cashflows.
        disc_curve_foreign : Curve
            The discounting :class:`Curve` for foreign currency cashflows.
        fx_rate : float, optional
            The FX rate for valuing cashflows.
        fx_settlement : datetime, optional
            The date for settlement of ``fx_rate``. If spot then should be input as T+2.
            If `None`, is assumed to be immediate settlement.
        base : str, optional
            The base currency to express the NPV, either `"domestic"` or `"foreign"`.
            Set by default.
        """
        base = defaults.fx_swap_base if base is None else base
        f_0 = forward_fx(
            disc_curve_domestic.node_dates[0],
            disc_curve_domestic,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        fx = forward_fx(
            self.effective,
            disc_curve_domestic,
            disc_curve_foreign,
            f_0,
        )
        self._set_leg2_notional(fx)
        leg1_npv = self.leg1.npv(curve_domestic)
        leg2_npv = self.leg2.npv(curve_foreign)

        if base == "foreign":
            return leg1_npv * f_0 + leg2_npv
        elif base == "domestic":
            return leg1_npv + leg2_npv / f_0
        else:
            raise ValueError('`base` should be either "domestic" or "foreign".')

    def _rate2(
        self,
        curve_domestic: Curve,
        disc_curve_domestic: Curve,
        curve_foreign: Curve,
        disc_curve_foreign: Curve,
        fx_rate: Union[float, Dual],
        fx_settlement: Optional[datetime] = None,
    ):  # pragma: no cover
        """
        Determine the mid-market floating spread on domestic leg 1, to equate leg 2.

        Parameters
        ----------
        curve_domestic : Curve
            The forecast :class:`Curve` for domestic currency cashflows.
        disc_curve_domestic : Curve
            The discount :class:`Curve` for domestic currency cashflows.
        curve_foreign : Curve
            The forecasting :class:`Curve` for foreign currency cashflows.
        disc_curve_foreign : Curve
            The discounting :class:`Curve` for foreign currency cashflows.
        fx_rate : float, optional
            The FX rate for valuing cashflows.
        fx_settlement : datetime, optional
            The date for settlement of ``fx_rate``. If spot then should be input as T+2.
            If `None`, is assumed to be immediate settlement.

        Returns
        -------
        BP Spread to leg 1 : Dual
        """
        npv = self.npv(
            curve_domestic,
            disc_curve_domestic,
            curve_foreign,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        f_0 = forward_fx(
            disc_curve_domestic.node_dates[0],
            disc_curve_domestic,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        leg1_analytic_delta = f_0 * self.leg1.analytic_delta(curve_domestic, disc_curve_domestic)
        spread = npv / leg1_analytic_delta
        return spread

    def _cashflows2(
        self,
        curve_domestic: Optional[Curve] = None,
        disc_curve_domestic: Optional[Curve] = None,
        curve_foreign: Optional[Curve] = None,
        disc_curve_foreign: Optional[Curve] = None,
        fx_rate: Optional[Union[float, Dual]] = None,
        fx_settlement: Optional[datetime] = None,
        base: Optional[str] = None,
    ):  # pragma: no cover
        """
        Return the properties of all legs used in calculating cashflows.

        Parameters
        ----------
        curve_domestic : Curve
            The forecast :class:`Curve` for domestic currency cashflows.
        disc_curve_domestic : Curve
            The discount :class:`Curve` for domestic currency cashflows.
        curve_foreign : Curve
            The forecasting :class:`Curve` for foreign currency cashflows.
        disc_curve_foreign : Curve
            The discounting :class:`Curve` for foreign currency cashflows.
        fx_rate : float, optional
            The FX rate for valuing cashflows.
        fx_settlement : datetime, optional
            The date for settlement of ``fx_rate``. If spot then should be input as T+2.
            If `None`, is assumed to be immediate settlement.
        base : str, optional
            The base currency to express the NPV, either `"domestic"` or `"foreign"`.
            Set by default.

        Returns
        -------
        DataFrame
        """
        f_0 = forward_fx(
            disc_curve_domestic.node_dates[0],
            disc_curve_domestic,
            disc_curve_foreign,
            fx_rate,
            fx_settlement
        )
        base = defaults.fx_swap_base if base is None else base
        if base == "foreign":
            d_fx, f_fx = f_0, 1.0
        elif base == "domestic":
            d_fx, f_fx = 1.0,  1.0 / f_0
        else:
            raise ValueError('`base` should be either "domestic" or "foreign".')
        self._set_leg2_notional(f_0)
        return concat([
            self.leg1.cashflows(curve_domestic, disc_curve_domestic, d_fx),
            self.leg2.cashflows(curve_foreign, disc_curve_foreign, f_fx),
        ], keys=["leg1", "leg2"],
        )


class FixedFloatXCS(BaseXCS):
    _fixed_rate_mixin = True
    _leg2_float_spread_mixin = True
    _is_mtm = True

    def __init__(
            self,
            *args,
            fx_fixings: Union[list, float, Dual, Dual2] = [],
            fixed_rate: Optional[float] = None,
            payment_lag_exchange: Optional[int] = None,
            leg2_float_spread: Optional[float] = None,
            leg2_fixings: Optional[Union[float, list]] = None,
            leg2_fixing_method: Optional[str] = None,
            leg2_method_param: Optional[int] = None,
            leg2_spread_compound_method: Optional[str] = None,
            leg2_payment_lag_exchange: Optional[int] = "inherit",
            **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        if fx_fixings is None:
            raise ValueError(
                "`fx_fixings` for MTM XCS should be entered as an empty list, not None."
            )
        self._fx_fixings = fx_fixings
        self._leg2_float_spread = leg2_float_spread
        self._fixed_rate = fixed_rate
        self.leg1 = FixedLegExchange(
            fixed_rate=fixed_rate,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FloatLegExchangeMtm(
            float_spread=leg2_float_spread,
            fixings=leg2_fixings,
            fixing_method=leg2_fixing_method,
            method_param=leg2_method_param,
            spread_compound_method=leg2_spread_compound_method,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            currency=self.leg2_currency,
            alt_currency=self.currency,
            alt_notional=-self.notional,
            fx_fixings=fx_fixings,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )


class FixedFixedXCS(BaseXCS):
    _fixed_rate_mixin = True
    _leg2_fixed_rate_mixin = True
    _is_mtm = True

    def __init__(
            self,
            *args,
            fx_fixings: Union[list, float, Dual, Dual2] = [],
            fixed_rate: Optional[float] = None,
            payment_lag_exchange: Optional[int] = None,
            leg2_fixed_rate: Optional[float] = None,
            leg2_payment_lag_exchange: Optional[int] = "inherit",
            **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        if fx_fixings is None:
            raise ValueError(
                "`fx_fixings` for MTM XCS should be entered as an empty list, not None."
            )
        self._fx_fixings = fx_fixings
        self._leg2_fixed_rate = leg2_fixed_rate
        self._fixed_rate = fixed_rate
        self.leg1 = FixedLegExchange(
            fixed_rate=fixed_rate,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FixedLegExchangeMtm(
            fixed_rate=leg2_fixed_rate,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            currency=self.leg2_currency,
            alt_currency=self.currency,
            alt_notional=-self.notional,
            fx_fixings=fx_fixings,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )


class FloatFixedXCS(BaseXCS):
    _float_spread_mixin = True
    _leg2_fixed_rate_mixin = True
    _is_mtm = True

    def __init__(
            self,
            *args,
            fx_fixings: Union[list, float, Dual, Dual2] = [],
            float_spread: Optional[float] = None,
            fixings: Optional[Union[float, list]] = None,
            fixing_method: Optional[str] = None,
            method_param: Optional[int] = None,
            spread_compound_method: Optional[str] = None,
            payment_lag_exchange: Optional[int] = None,
            leg2_fixed_rate: Optional[float] = None,
            leg2_payment_lag_exchange: Optional[int] = "inherit",
            **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        if fx_fixings is None:
            raise ValueError(
                "`fx_fixings` for MTM XCS should be entered as an empty list, not None."
            )
        self._fx_fixings = fx_fixings
        self._leg2_fixed_rate = leg2_fixed_rate
        self._float_spread = float_spread
        self.leg1 = FloatLegExchange(
            float_spread=float_spread,
            fixings=fixings,
            fixing_method=fixing_method,
            method_param=method_param,
            spread_compound_method=spread_compound_method,
            effective=self.effective,
            termination=self.termination,
            frequency=self.frequency,
            stub=self.stub,
            front_stub=self.front_stub,
            back_stub=self.back_stub,
            roll=self.roll,
            eom=self.eom,
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=self.payment_lag,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            amortization=self.amortization,
            convention=self.convention,
        )
        self.leg2 = FixedLegExchangeMtm(
            fixed_rate=leg2_fixed_rate,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency=self.leg2_frequency,
            stub=self.leg2_stub,
            front_stub=self.leg2_front_stub,
            back_stub=self.leg2_back_stub,
            roll=self.leg2_roll,
            eom=self.leg2_eom,
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=self.leg2_payment_lag,
            payment_lag_exchange=leg2_payment_lag_exchange,
            currency=self.leg2_currency,
            alt_currency=self.currency,
            alt_notional=-self.notional,
            fx_fixings=fx_fixings,
            amortization=self.leg2_amortization,
            convention=self.leg2_convention,
        )


class FXSwap(BaseXCS):
    _fixed_rate_mixin = True
    _leg2_fixed_rate_mixin = True

    """
    Create n FX swap simulated via a :class:`NonMtmFixedFixedXCS`.

    Parameters
    ----------
    args : dict
        Required positional args to :class:`BaseDerivative`.
    fx_fixing : float, FXForwards or None
        The initial FX fixing where leg 1 is considered the domestic currency. For
        example for an ESTR/SOFR XCS in 100mm EUR notional a value of 1.10 for `fx0`
        implies the notional on leg 2 is 110m USD. If `None` determines this
        dynamically.
    payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    leg2_fixed_rate : float or None
        The fixed rate applied to leg 2.
        If `None` will be set to mid-market when curves are provided.
        Must set the ``fixed_rate`` on at least one leg.
    leg2_payment_lag_exchange : int
        The number of business days by which to delay notional exchanges, aligned with
        the accrual schedule.
    kwargs : dict
        Required keyword arguments to :class:`BaseDerivative`.

    Notes
    -----
    TODO XXXXXXX
    """

    def __init__(
        self,
        *args,
        fx_fixing: Optional[Union[float, FXRates, FXForwards]] = None,
        payment_lag_exchange: Optional[int] = None,
        leg2_fixed_rate: Optional[float] = None,
        leg2_payment_lag_exchange: Optional[int] = "inherit",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if leg2_payment_lag_exchange == "inherit":
            leg2_payment_lag_exchange = payment_lag_exchange
        self._leg2_fixed_rate = leg2_fixed_rate
        self._fixed_rate = 0.0
        self.leg1 = FixedLegExchange(
            fixed_rate=0.0,
            effective=self.effective,
            termination=self.termination,
            frequency="Z",
            modifier=self.modifier,
            calendar=self.calendar,
            payment_lag=payment_lag_exchange,
            payment_lag_exchange=payment_lag_exchange,
            notional=self.notional,
            currency=self.currency,
            convention=self.convention,
        )
        self.leg2 = FixedLegExchange(
            fixed_rate=leg2_fixed_rate,
            effective=self.leg2_effective,
            termination=self.leg2_termination,
            frequency="Z",
            modifier=self.leg2_modifier,
            calendar=self.leg2_calendar,
            payment_lag=leg2_payment_lag_exchange,
            payment_lag_exchange=leg2_payment_lag_exchange,
            notional=self.leg2_notional,
            currency=self.leg2_currency,
            convention=self.leg2_convention,
        )
        self._initialise_fx_fixings(fx_fixing)

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.

    def rate(
        self,
        curves: Union[Curve, str, list],
        solver: Optional[Solver] = None,
        fx: Optional[FXForwards] = None,
        fixed_rate: bool = False,
    ):
        """
        Return the mid-market pricing parameter of the FXSwap.

        Parameters
        ----------
        curves : list of Curves
            A list defines the following curves in the order:

            - Forecasting :class:`~rateslib.curves.Curve` for leg1 (if floating).
            - Discounting :class:`~rateslib.curves.Curve` for leg1.
            - Forecasting :class:`~rateslib.curves.Curve` for leg2 (if floating).
            - Discounting :class:`~rateslib.curves.Curve` for leg2.
        solver : Solver, optional
            The numerical :class:`~rateslib.solver.Solver` that
            constructs :class:`~rateslib.curves.Curve` from calibrating instruments.
        fx : FXForwards, optional
            The FX forwards object that is used to determine the initial FX fixing for
            determining ``leg2_notional``, if not specified at initialisation, and for
            determining mark-to-market exchanges on mtm XCSs.

        Returns
        -------
        float, Dual or Dual2

        Notes
        -----
        Fixed legs have pricing parameter returned in percentage terms, and
        float legs have pricing parameter returned in basis point (bp) terms.

        If the ``XCS`` type is specified without a ``fixed_rate`` on any leg then an
        implied ``float_spread`` will return as its originaly value or zero since
        the fixed rate used
        for calculation is the implied mid-market rate including the
        current ``float_spread`` parameter.

        Examples
        --------
        """
        leg2_fixed_rate = super().rate(curves, solver, fx, leg=2)
        if fixed_rate:
            return leg2_fixed_rate
        cf = self.leg2.notional * leg2_fixed_rate * 0.01 * self.leg2.periods[1].dcf
        # fwd_fx = (cf + self.leg2.notional) / -self.leg1.notional
        # ini_fx = self.leg2.notional / -self.leg1.notional
        ## TODO decide how to price mid-market rates when ini fx is struck but
        ## there is no fixed points, i,e the FXswap is sem-determined, which is
        ## not a real instrument.
        return (cf / -self.leg1.notional) * 10000


# class FXSwap(BaseDerivative):
#     """
#     Create an FX Swap instrument.
#
#     ``FXSwap`` is a multi-currency derivative. Must supply the ``currency`` and
#     ``leg2_currency`` as 3-digit codes, for consistent pricing.
#
#     Parameters
#     ----------
#     args : tuple
#         Required positional args to :class:`BaseDerivative`.
#     fx_fixing_points: tuple(float or Dual,), optional
#        A tuple of two floats (or Duals): the FX rate for the initial exchange applied to
#        ``notional`` to directly set ``leg2_notional``, and the fx swap points (in
#        10,000ths) which will directly set the foreign cashflow at termination.
#
#        .. warning::
#
#           Do not set ``leg2_notional`` directly as an input. Use ``fx_fixing_points`` to
#           determine the foreign leg notional and cashflows at the settlement dates.
#
#     kwargs : dict
#         Required keyword arguments to :class:`BaseDerivative`.
#
#     Notes
#     -----
#
#     **Pricing Parameters**
#
#     Although an FX swap contains only 4 cashflows, its definition and operation are
#     made more complex by allowing pricing parameters to be set or unset at
#     initialisation. There are 2 possible initialisation states:
#
#       - Fixing ``fx0`` and ``points``: this is the common initialisation
#         for an executed FX swap trade. All pricing parameters are set and determined.
#       - Not fixing ``fx0`` or ``points``: this is the common initialisation for an
#         interbank trade. The instrument is defined except for its pricing parameters
#         which can be priced subject to other variables. Upon execution these parameters
#         would then be set.
#
#     **Cashflow Settlement**
#
#     The date generation for FXSwaps used the :class:`Schedule` generator. For FXSwaps
#     the default ``payment_lag`` is 0 days meaning payment dates align with accrual
#     start and end dates, so the ``effective`` and ``termination`` date will define the
#     payment dates (except if they are adjusted under a holiday calendar modification).
#     The settlement dates for FXSwap cashflows are determined from
#     the ``schedule.pschedule`` (payment day schedule) attribute of each leg.
#     """
#     def __init__(
#         self,
#         *args,
#         fx_fixing_points: Optional[tuple] = None,
#         **kwargs,
#     ):
#         super().__init__(*args, **kwargs)
#         if self.currency is None or self.leg2_currency is None:
#             raise ValueError("Must supply `currency` and `leg2_currency` to `FXSwap`.")
#         self.leg1, self.leg2 = CustomLeg([]), CustomLeg([])
#         self.leg1.currency,  self.leg2.currency = self.currency, self.leg2_currency
#         self.pair = self.currency + self.leg2_currency
#
#         self.leg1.schedule = Schedule(
#             effective=self.effective,
#             termination=self.termination,
#             frequency="Z",
#             modifier=self.modifier,
#             calendar=self.calendar,
#             payment_lag=self.payment_lag,
#         )
#         self.leg2.schedule = Schedule(
#             effective=self.leg2_effective,
#             termination=self.leg2_termination,
#             frequency="Z",
#             modifier=self.leg2_modifier,
#             calendar=self.leg2_calendar,
#             payment_lag=self.leg2_payment_lag,
#         )
#         self._fx_fixing_points = fx_fixing_points
#
#         if self.notional != -self.leg2_notional:
#             Warning("`leg2_notional` is not used in the FXSwap class.")
#         self.leg1.periods = [
#             Cashflow(
#                 notional=self.notional,
#                 payment=self.leg1.schedule.pschedule[0],
#                 currency=self.leg1.currency
#             ),
#             Cashflow(
#                 notional=-self.notional,
#                 payment=self.leg1.schedule.pschedule[1],
#                 currency=self.leg1.currency
#             )
#         ]
#         self.fx_fixing_points = fx_fixing_points  # sets leg2
#
#     @property
#     def fx_fixing_points(self):
#         """Sets the FX fixing and the swap points to define the cashflows."""
#         return self._fx_fixing_points
#
#     @fx_fixing_points.setter
#     def fx_fixing_points(self, value):
#         """Will reset leg2 notional and cashflows. Only called if not set at init."""
#         self._fx_fixing_points = value
#         if value is None:
#             fx0, points = 1.0, 0.0
#         else:
#             fx0, points = value[0], value[1]
#         self._set_cashflows(fx0, points)
#
#     def _set_cashflows(self, fx0, points):
#         self.leg2_notional = -self.notional * fx0
#         self.leg2.periods = [
#             Cashflow(notional=self.leg2_notional,
#                      payment=self.leg2.schedule.pschedule[0],
#                      currency=self.leg2.currency),
#             Cashflow(notional=self.notional * (fx0 + points / 10000),
#                      payment=self.leg2.schedule.pschedule[1],
#                      currency=self.leg2.currency)
#         ]
#
#     def npv(
#         self,
#         fx: Optional[FXForwards] = None,
#         solver: Optional[Solver] = None,
#         collateral: Optional[str] = None,
#         base: Optional[str] = None,
#     ):
#         """
#         Return the NPV of the FXSwap.
#
#         An ``FXSwap`` is a multi-currency derivative so an
#         :class:`~rateslib.fx.FXForwards` object is required for pricing which
#         contains all the necessary cross-currency discount factors.
#
#         Parameters
#         ----------
#         fx
#         solver
#         collateral
#         base
#
#         Returns
#         -------
#         float, Dual or Dual2
#         """
#         if fx is None:
#             if solver is None or solver.fx is None:
#                 raise ValueError("`fx` or `solver.fx` must be supplied")
#             elif solver.fx is not None:
#                 fx = solver.fx
#
#         base = fx.base if base is None else base.lower()
#         if self.fx_fixing_points is None:
#             fx0 = fx.rate(self.pair, self.leg1.schedule.aschedule[0])
#             points = self.rate(fx)
#             self._set_cashflows(float(fx0), float(points))
#
#         leg1_npv = (
#             fx.curve(self.leg1.currency, collateral)[self.leg1.periods[0].payment] *
#             self.leg1.periods[0].notional + self.leg1.periods[1].notional *
#             fx.curve(self.leg1.currency, collateral)[self.leg1.periods[1].payment]
#         )
#         leg2_npv = (
#             fx.curve(self.leg2.currency, collateral)[self.leg2.periods[0].payment] *
#             self.leg2.periods[0].notional + self.leg2.periods[1].notional *
#             fx.curve(self.leg2.currency, collateral)[self.leg2.periods[1].payment]
#         )
#
#         return (
#             fx.rate(self.leg1.currency + base) * leg1_npv +
#             fx.rate(self.leg2.currency + base) * leg2_npv
#         )
#
#     def _npv_alt(
#         self,
#         curve_domestic: Curve,
#         curve_foreign: Curve,
#         fx_rate: Union[float, Dual],
#         fx_settlement: Optional[datetime] = None,
#         base: str = None
#     ):
#         """
#         Return the NPV of the FX swap.
#
#         Parameters
#         ----------
#         curve_domestic : Curve
#             The discounting :class:`Curve` for domestic currency cashflows.
#         curve_foreign : Curve
#             The discounting :class:`Curve` for foreign currency cashflows.
#         fx_rate : float, optional
#             The FX rate for valuing cashflows.
#         fx_settlement : datetime, optional
#             The date for settlement of ``fx_rate``. If spot then should be input as T+2.
#             If `None`, is assumed to be immediate settlement.
#         base : str, optional
#             The base currency to express the NPV, either `"domestic"` or `"foreign"`.
#             Set by default.
#         """
#         base = defaults.fx_swap_base if base is None else base
#         f_0 = forward_fx(
#             curve_domestic.node_dates[0],
#             curve_domestic,
#             curve_foreign,
#             fx_rate,
#             fx_settlement
#         )
#         if self.fx_fixing_points is None:
#             args = (curve_domestic, curve_foreign, f_0)
#             fx = forward_fx(self.leg2.schedule.aschedule[0], *args)
#             points = self._rate_alt(*args)
#             self._set_cashflows(fx, points)
#         leg1_npv = self.leg1.npv(curve_domestic)
#         leg2_npv = self.leg2.npv(curve_foreign)
#
#         if base == "foreign":
#             return leg1_npv * f_0 + leg2_npv
#         elif base == "domestic":
#             return leg1_npv + leg2_npv / f_0
#         else:
#             raise ValueError('`base` should be either "domestic" or "foreign".')
#
#     def rate(
#         self,
#         fx: Optional[FXForwards] = None,
#         solver: Optional[Solver] = None,
#     ):
#         if fx is None:
#             if solver is None or solver.fx is None:
#                 raise ValueError("`fx` or `solver.fx` must be supplied")
#             elif solver.fx is not None:
#                 fx = solver.fx
#
#         fx0 = fx.rate(self.pair, settlement=self.leg1.periods[0].payment)
#         fx1 = fx.rate(self.pair, settlement=self.leg1.periods[1].payment)
#         return (fx1 - fx0) * 10000
#
#     def _rate_alt(
#         self,
#         curve_domestic: Curve,
#         curve_foreign: Curve,
#         fx_rate: Optional[Union[float, Dual]] = None,
#         fx_settlement: Optional[datetime] = None,
#     ):
#         """
#         Return the mid-market rate in points of the FX swap.
#
#         Parameters
#         ----------
#         curve_domestic : Curve
#             The discounting :class:`Curve` for domestic currency cashflows.
#         curve_foreign : Curve
#             The discounting :class:`Curve` for foreign currency cashflows.
#         fx_rate : float or Dual, optional
#             The FX rate for valuing cashflows.
#         fx_settlement : datetime, optional
#             The date for settlement of ``fx_rate``. If spot then should be input as T+2.
#             If `None`, is assumed to be immediate settlement.
#
#         Returns
#         -------
#         Dual
#
#         Notes
#         -----
#
#         .. math::
#
#            z_{fx} = f_{i-1} \\left ( \\frac{v_{i-1}-v_i}{v_i} \\right ) + F_0 \\left ( \\frac{w_j^* - w_{j-1}^*}{v_i} \\right )
#
#         where, :math:`v_{i-1}, v_i` denote DFs on the dates of exchange of the foreign
#         currency, and, :math:`w_j^*, w_{j-1}^*` denote dates of exchange of the
#         domestic currency, which in an FX swap should align.
#         """
#         f_0 = forward_fx(
#             curve_domestic.node_dates[0],
#             curve_domestic,
#             curve_foreign,
#             fx_rate,
#             fx_settlement
#         )
#         args = (curve_domestic, curve_foreign, f_0)
#         f_i_1 = forward_fx(self.leg2.periods[0].payment, *args)
#         v_i_1 = curve_foreign[self.leg2.periods[0].payment]
#         v_i = curve_foreign[self.leg2.periods[1].payment]
#         w_j_1 = curve_domestic[self.leg1.periods[0].payment]
#         w_j = curve_domestic[self.leg1.periods[1].payment]
#         _ = (f_i_1 * (v_i_1 - v_i) + f_0 * (w_j - w_j_1)) / v_i
#         return _ * 10000
#
#     # TODO make FXSwap cashflow arguments consistent with theme
#     def cashflows(self, curve_domestic, curve_foreign):
#         return super().cashflows(None, curve_domestic, None, curve_foreign)
#
#     def delta(
#         self,
#         solver: Optional[Solver] = None,
#         collateral: Optional[str] = None,
#         base: Optional[str] = None,
#     ):
#         npv = self.npv(None, collateral, solver, base)
#         return solver.delta(npv)


### Generic Instruments


class Spread(Sensitivities):
    """
    A spread instrument defined as the difference in rate between two ``Instruments``.

    The ``Instruments`` used must share common pricing arguments. See notes.

    Parameters
    ----------
    instrument1 : Instrument
        The initial instrument, usually the shortest tenor, e.g. 5Y in 5s10s.
    instrument2 : Instrument
        The second instrument, usually the longest tenor, e.g. 10Y in 5s10s.

    Notes
    -----
    When using :class:`Spread` both ``Instruments`` must be of the same type
    with shared pricing arguments for their methods. If this is not true
    consider using the :class:`SpreadX`, cross spread ``Instrument``.

    Examples
    --------
    Creating a dynamic :class:`Spread` where the instruments are dynamically priced,
    and each share the pricing arguments.

    .. ipython:: python

       curve1 = Curve({dt(2022, 1, 1): 1.0, dt(2022, 4, 1):0.995, dt(2022, 7, 1):0.985})
       irs1 = IRS(dt(2022, 1, 1), "3M", "Q")
       irs2 = IRS(dt(2022, 1, 1), "6M", "Q")
       spread = Spread(irs1, irs2)
       spread.npv(curve1)
       spread.rate(curve1)
       spread.cashflows(curve1)

    Creating an assigned :class:`Spread`, where each ``Instrument`` has its own
    assigned pricing arguments.

    .. ipython:: python

       curve1 = Curve({dt(2022, 1, 1): 1.0, dt(2022, 4, 1):0.995, dt(2022, 7, 1):0.985})
       curve2 = Curve({dt(2022, 1, 1): 1.0, dt(2022, 4, 1):0.99, dt(2022, 7, 1):0.98})
       irs1 = IRS(dt(2022, 1, 1), "3M", "Q", curves=curve1)
       irs2 = IRS(dt(2022, 1, 1), "6M", "Q", curves=curve2)
       spread = Spread(irs1, irs2)
       spread.npv()
       spread.rate()
       spread.cashflows()
    """
    def __init__(self, instrument1, instrument2):
        self.instrument1 = instrument1
        self.instrument2 = instrument2

    def npv(self, *args, **kwargs):
        """
        Return the NPV of the composited object by summing instrument NPVs.

        Parameters
        ----------
        args :
            Positional arguments required for the ``npv`` method of both of the
            underlying ``Instruments``.
        kwargs :
            Keyword arguments required for the ``npv`` method of both of the underlying
            ``Instruments``.

        Returns
        -------
        float, Dual or Dual2
        """
        leg1_npv = self.instrument1.npv(*args, **kwargs)
        leg2_npv = self.instrument2.npv(*args, **kwargs)
        return leg1_npv + leg2_npv

    # def npv(self, *args, **kwargs):
    #     if len(args) == 0:
    #         args1 = (kwargs.get("curve1", None), kwargs.get("disc_curve1", None))
    #         args2 = (kwargs.get("curve2", None), kwargs.get("disc_curve2", None))
    #     else:
    #         args1 = args
    #         args2 = args
    #     return self.instrument1.npv(*args1) + self.instrument2.npv(*args2)

    def rate(self, *args, **kwargs):
        """
        Return the mid-market rate of the composited via the difference of instrument
        rates.

        Parameters
        ----------
        args :
            Positional arguments required for the ``rate`` method of both of the
            underlying ``Instruments``.
        kwargs :
            Keyword arguments required for the ``rate`` method of both of the underlying
            ``Instruments``.

        Returns
        -------
        float, Dual or Dual2
        """
        leg1_rate = self.instrument1.rate(*args, **kwargs)
        leg2_rate = self.instrument2.rate(*args, **kwargs)
        return leg2_rate - leg1_rate

    # def rate(self, *args, **kwargs):
    #     if len(args) == 0:
    #         args1 = (kwargs.get("curve1", None), kwargs.get("disc_curve1", None))
    #         args2 = (kwargs.get("curve2", None), kwargs.get("disc_curve2", None))
    #     else:
    #         args1 = args
    #         args2 = args
    #     return self.instrument2.rate(*args2) - self.instrument1.rate(*args1)

    def cashflows(self, *args, **kwargs):
        return concat([
            self.instrument1.cashflows(*args, **kwargs),
            self.instrument2.cashflows(*args, **kwargs),
            ], keys=["instrument1", "instrument2"],
        )


# class SpreadX:
#     pass


class Fly(Sensitivities):
    """
    A butterfly instrument which is, mechanically, the spread of two spread instruments.

    The ``Instruments`` used must share common dynamic pricing arguments
    or be statically created. See notes XXXX link o pricingmechanisms.

    Parameters
    ----------
    instrument1 : Instrument
        The initial instrument, usually the shortest tenor, e.g. 5Y in 5s10s15s.
    instrument2 : Instrument
        The second instrument, usually the mid-length tenor, e.g. 10Y in 5s10s15s.
    instrument3 : Instrument
        The third instrument, usually the longest tenor, e.g. 15Y in 5s10s15s.

    Notes
    -----
    When using :class:`Spread` both ``Instruments`` must be of the same type
    with shared pricing arguments for their methods. If this is not true
    consider using the :class:`FlyX`, cross ``Instrument``.

    Examples
    --------
    See examples for :class:`Spread` for similar functionality.
    """
    def __init__(self, instrument1, instrument2, instrument3):
        self.instrument1 = instrument1
        self.instrument2 = instrument2
        self.instrument3 = instrument3

    def npv(self, *args, **kwargs):
        """
        Return the NPV of the composited object by summing instrument NPVs.

        Parameters
        ----------
        args :
            Positional arguments required for the ``npv`` method of both of the
            underlying ``Instruments``.
        kwargs :
            Keyword arguments required for the ``npv`` method of both of the underlying
            ``Instruments``.

        Returns
        -------
        float, Dual or Dual2
        """
        leg1_npv = self.instrument1.npv(*args, **kwargs)
        leg2_npv = self.instrument2.npv(*args, **kwargs)
        leg3_npv = self.instrument3.npv(*args, **kwargs)
        return leg1_npv + leg2_npv + leg3_npv

    def rate(self, *args, **kwargs):
        """
        Return the mid-market rate of the composited via the difference of instrument
        rates.

        Parameters
        ----------
        args :
            Positional arguments required for the ``rate`` method of both of the
            underlying ``Instruments``.
        kwargs :
            Keyword arguments required for the ``rate`` method of both of the underlying
            ``Instruments``.

        Returns
        -------
        float, Dual or Dual2
        """
        leg1_rate = self.instrument1.rate(*args, **kwargs)
        leg2_rate = self.instrument2.rate(*args, **kwargs)
        leg3_rate = self.instrument3.rate(*args, **kwargs)
        return -leg3_rate + 2 * leg2_rate - leg1_rate

    def cashflows(self, *args, **kwargs):
        return concat([
            self.instrument1.cashflows(*args, **kwargs),
            self.instrument2.cashflows(*args, **kwargs),
            self.instrument3.cashflows(*args, **kwargs),
            ], keys=["instrument1", "instrument2", "instrument3"],
        )


# class FlyX:
#     """
#     A butterly instrument which is the spread of two spread instruments
#     """
#     def __init__(self, instrument1, instrument2, instrument3):
#         self.instrument1 = instrument1
#         self.instrument2 = instrument2
#         self.instrument3 = instrument3
#
#     def npv(self, *args, **kwargs):
#         if len(args) == 0:
#             args1 = (kwargs.get("curve1", None), kwargs.get("disc_curve1", None))
#             args2 = (kwargs.get("curve2", None), kwargs.get("disc_curve2", None))
#             args3 = (kwargs.get("curve3", None), kwargs.get("disc_curve3", None))
#         else:
#             args1 = args
#             args2 = args
#             args3 = args
#         return self.instrument1.npv(*args1) + self.instrument2.npv(*args2) + self.instrument3.npv(*args3)
#
#     def rate(self, *args, **kwargs):
#         if len(args) == 0:
#             args1 = (kwargs.get("curve1", None), kwargs.get("disc_curve1", None))
#             args2 = (kwargs.get("curve2", None), kwargs.get("disc_curve2", None))
#             args3 = (kwargs.get("curve3", None), kwargs.get("disc_curve3", None))
#         else:
#             args1 = args
#             args2 = args
#             args3 = args
#         return 2 * self.instrument2.rate(*args2) - self.instrument1.rate(*args1) - self.instrument3.rate(*args3)


class Portfolio(Sensitivities):

    def __init__(self, instruments):
        self.instruments = instruments

    def npv(self, *args, **kwargs):
        # TODO do not permit a mixing of currencies.
        _ = 0
        for instrument in self.instruments:
            _ += instrument.npv(*args, **kwargs)
        return _


def forward_fx(
    date: datetime,
    curve_domestic: Curve,
    curve_foreign: Curve,
    fx_rate: Union[float, Dual],
    fx_settlement: Optional[datetime] = None,
) -> Dual:
    """
    Return the adjusted FX rate based on interest rate parity.

    .. deprecated:: 0.0
       See notes.

    Parameters
    ----------
    date : datetime
        The target date to determine the adjusted FX rate for.
    curve_domestic : Curve
        The discount curve for the domestic currency. Should be FX swap / XCS adjusted.
    curve_foreign : Curve
        The discount curve for the foreign currency. Should be FX swap / XCS consistent
        with ``domestic curve``.
    fx_rate : float or Dual
        The known FX rate, typically spot FX given with a spot settlement date.
    fx_settlement : datetime, optional
        The date the given ``fx_rate`` will settle, i.e spot T+2. If `None` is assumed
        to be immediate settlement, i.e. date upon which both ``curves`` have a DF
        of precisely 1.0. Method is more efficient if ``fx_rate`` is given for
        immediate settlement.

    Returns
    -------
    float, Dual, Dual2

    Notes
    -----
    We use the formula,

    .. math::

       (EURUSD) f_i = \\frac{(EUR:USD-CSA) w^*_i}{(USD:USD-CSA) v_i} F_0 = \\frac{(EUR:EUR-CSA) v^*_i}{(USD:EUR-CSA) w_i} F_0

    where :math:`w` is a cross currency adjusted discount curve and :math:`v` is the
    locally derived discount curve in a given currency, and `*` denotes the domestic
    currency. :math:`F_0` is the immediate FX rate, i.e. aligning with the initial date
    on curves such that discounts factors are precisely 1.0.

    This implies that given the dates and rates supplied,

    .. math::

       f_i = \\frac{w^*_iv_j}{v_iw_j^*} f_j = \\frac{v^*_iw_j}{w_iv_j^*} f_j

    where `j` denotes the settlement date provided.

    **Deprecated**

    This method is deprecated. It should be replaced by the use of
    :class:`~rateslib.fx.FXForwards` objects. See examples.

    Examples
    --------
    Using this function directly.

    .. ipython:: python

       domestic_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.96})
       foreign_curve = Curve({dt(2022, 1, 1): 1.0, dt(2023, 1, 1): 0.99})
       forward_fx(
           date=dt(2022, 7, 1),
           curve_domestic=domestic_curve,
           curve_foreign=foreign_curve,
           fx_rate=2.0,
           fx_settlement=dt(2022, 1, 3)
       )

    Replacing this deprecated function with object-oriented methods.

    .. ipython:: python

       fxr = FXRates({"usdgbp": 2.0}, settlement=dt(2022, 1, 3))
       fxf = FXForwards(fxr, {
           "usdusd": domestic_curve,
           "gbpgbp": foreign_curve,
           "gbpusd": foreign_curve,
       })
       fxf.rate("usdgbp", dt(2022, 7, 1))
    """
    if date == fx_settlement:
        return fx_rate
    elif date == curve_domestic.node_dates[0] and fx_settlement is None:
        return fx_rate

    _ = curve_domestic[date] / curve_foreign[date]
    if fx_settlement is not None:
        _ *= curve_foreign[fx_settlement] / curve_domestic[fx_settlement]
    _ *= fx_rate
    return _

# Licence: Creative Commons - Attribution-NonCommercial-NoDerivatives 4.0 International
# Commercial use of this code, and/or copying and redistribution is prohibited.
# Contact rateslib at gmail.com if this code is observed outside its intended sphere.