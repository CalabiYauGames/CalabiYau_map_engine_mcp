"""`quad_to_transform`: four corners in, one item-7 pose out.

`place_quad` promises that a caller may hand over the four corners of a planar
rectangle "in any order", so that promise is what most of this file pins. The
canonical order is pinned too, because it is the one the documentation uses in
its examples and its numbers must not drift.
"""

from __future__ import annotations

import itertools
import math

import pytest

from calabiyau_map_engine_mcp.tools import quad_to_transform

# Item 7's face is 1 m x 1 m and 0.25 m thick, and a fitted plate is given a
# near-zero thickness: 1e-4 cm over the 25 cm the local box allows.
PLATE_THICKNESS_SCALE = 4e-6


def horizontal() -> list[list[float]]:
    """A 200 cm x 150 cm rectangle lying in the XY plane."""
    return [[-100, -75, 0], [100, -75, 0], [-100, 75, 0], [100, 75, 0]]


def test_a_ground_rectangle_becomes_a_flat_plate() -> None:
    transform = quad_to_transform(horizontal())

    assert transform["location_cm"] == pytest.approx([0, 0, 0])
    assert transform["scale"] == pytest.approx([PLATE_THICKNESS_SCALE, 2.0, 1.5])
    # A face in the XY plane points along +Z, so pitch is 90 degrees.
    assert transform["rotation_degrees"] == pytest.approx([90, 0, 90])


def test_a_vertical_rectangle_keeps_its_width_and_height() -> None:
    transform = quad_to_transform(
        [[-100, 0, -75], [100, 0, -75], [-100, 0, 75], [100, 0, 75]]
    )

    assert transform["location_cm"] == pytest.approx([0, 0, 0])
    assert transform["scale"] == pytest.approx([PLATE_THICKNESS_SCALE, 2.0, 1.5])
    assert transform["rotation_degrees"] == pytest.approx([0, -90, 0])


def test_a_square_turned_45_degrees_keeps_its_side_lengths() -> None:
    side = 100.0
    offset = side / math.sqrt(2)
    transform = quad_to_transform(
        [[0, -offset, 0], [offset, 0, 0], [-offset, 0, 0], [0, offset, 0]]
    )

    assert transform["location_cm"] == pytest.approx([0, 0, 0])
    assert transform["scale"] == pytest.approx([PLATE_THICKNESS_SCALE, 1.0, 1.0])
    assert transform["rotation_degrees"] == pytest.approx([90, 0, 45])


def test_the_corner_order_does_not_change_which_face_is_built() -> None:
    """Every one of the 24 orderings has to describe the same rectangle.

    This is the regression: the old edge-length test accepted an ordering whose
    first edge was the diagonal and then rejected a perfectly good rectangle.
    """
    corners = horizontal()
    centres = set()
    sizes = set()
    for order in itertools.permutations(range(4)):
        transform = quad_to_transform([corners[index] for index in order])
        centres.add(tuple(round(axis, 6) for axis in transform["location_cm"]))
        # Width and height may swap with the ordering; the pair may not.
        sizes.add(tuple(sorted(round(axis, 6) for axis in transform["scale"][1:])))

    assert centres == {(0.0, 0.0, 0.0)}
    assert sizes == {(1.5, 2.0)}


def test_a_trapezoid_is_refused() -> None:
    with pytest.raises(ValueError, match="does not close it"):
        quad_to_transform([[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 50, 0]])


def test_a_kite_is_refused() -> None:
    with pytest.raises(ValueError, match="not perpendicular"):
        quad_to_transform([[0, 0, 0], [100, 0, 0], [50, 80, 0], [50, -20, 0]])


def test_coincident_corners_are_refused() -> None:
    with pytest.raises(ValueError, match="no single diagonal"):
        quad_to_transform([[0, 0, 0], [0, 0, 0], [100, 0, 0], [100, 0, 0]])


def test_two_dimensional_corners_are_refused() -> None:
    with pytest.raises(ValueError, match="three components"):
        quad_to_transform([[0, 0], [1, 0], [1, 1], [0, 1]])


def test_the_wrong_number_of_corners_is_refused() -> None:
    with pytest.raises(ValueError, match="Expected 4 vertices"):
        quad_to_transform([[0, 0, 0], [1, 0, 0], [0, 1, 0]])
