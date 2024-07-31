from typing import Union

from rateslib.dual import Dual, Dual2
from rateslib.rs import PPSplineDual, PPSplineDual2, PPSplineF64, bspldnev_single, bsplev_single
from rateslib.rs import PPSplineF64 as PPSpline
# for legacy reasons allow a PPSpline class which allows only f64 datatypes.

PPSplineF64.__doc__ = "Piecewise polynomial spline composed of float values on the x and y axes."
PPSplineDual.__doc__ = "Piecewise polynomial spline composed of float values on the x-axis and Dual values on the y-axis."
PPSplineDual2.__doc__ = "Piecewise polynomial spline composed of float values on the x-axis and Dual2 values on the y-axis."


def evaluate(
    spline: Union[PPSplineF64, PPSplineDual, PPSplineDual2],
    x: Union[float, Dual, Dual2],
    m: int = 0,
) -> Union[float, Dual, Dual2]:
    """
    Evaluate a single x-axis data point, or a derivative value, on a *Spline*.

    This method automatically calls :meth:`~rateslib.splines.PPSplineF64.ppdnev_single`,
    :meth:`~rateslib.splines.PPSplineF64.ppdnev_single_dual` or
    :meth:`~rateslib.splines.PPSplineF64.ppdnev_single_dual2`  based on the input form of ``x``.

    This method is AD safe.

    Parameters
    ----------
    spline: PPSplineF64, PPSplineDual, PPSplineDual2
        The *Spline* on which to evaluate the data point.
    x: float, Dual, Dual2
        The x-axis data point to evaluate.
    m: int, optional
        The order of derivative to evaluate. If seeking value only use *m=0*.

    Returns
    -------
    float, Dual, Dual2
    """
    if isinstance(x, Dual):
        return spline.ppdnev_single_dual(x, m)
    elif isinstance(x, Dual2):
        return spline.ppdnev_single_dual2(x, m)
    else:
        return spline.ppdnev_single(x, m)