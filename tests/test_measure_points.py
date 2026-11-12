"""#1118: a measurement names its points, as literals a script can use."""
import numpy as np

from belfryscad.window.viewport import Measurement, point_literal


def test_a_point_is_an_openscad_vector_without_float_noise():
    assert point_literal(np.array([10.0, 0.0, 2.5])) == "[10, 0, 2.5]"
    assert point_literal([1.000000001, -0.00000001, -3.14159]) == "[1, 0, -3.1416]"


def test_a_measurement_says_where_it_was_taken():
    d = Measurement("distance", [np.array([0.0, 0, 0]), np.array([10.0, 0, 5])], ["vertex", "vertex"])
    assert d.where() == "from [0, 0, 0] to [10, 0, 5]"
    a = Measurement("angle", [np.array([1.0, 0, 0]), np.array([0.0, 0, 0]), np.array([0.0, 1, 0])],
                    ["vertex"] * 3)
    assert a.where() == "at [0, 0, 0] between [1, 0, 0] and [0, 1, 0]"
