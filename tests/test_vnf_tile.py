"""`belfryscad.vnf_tile`: BOSL2's VNF tile seam check, and linked moves.

When the port was written, all 12 of BOSL2's own VNF textures (texture()
@ 9f1fa5c7) came back clean, and raising one edge vertex of each flagged
it and its twin.
"""

import copy

from belfryscad.vnf_tile import is_tile, linked_moves, tile_problems

# texture("diamonds_vnf")
DIAMONDS = [
    [[0, 1, 1], [0.5, 1, 0], [1, 1, 1], [0, 0.5, 0], [0.5, 0.5, 1], [1, 0.5, 0], [0, 0, 1], [0.5, 0, 0], [1, 0, 1]],
    [[0, 1, 3], [2, 5, 1], [8, 7, 5], [6, 3, 7], [1, 5, 4], [5, 7, 4], [7, 3, 4], [4, 3, 1]],
]


def test_bosl2_texture_tiles_cleanly():
    assert tile_problems(DIAMONDS) == ([], [])


def test_raised_edge_vertex_and_its_twin_are_both_unmatched():
    bad = copy.deepcopy(DIAMONDS)
    bad[0][3][2] = 0.2          # [0, 0.5, 0] no longer meets [1, 0.5, 0]
    assert tile_problems(bad) == ([], [3, 5])


def test_out_of_range_vertex_is_reported():
    bad = copy.deepcopy(DIAMONDS)
    bad[0][4][0] = 1.5
    assert tile_problems(bad)[0] == [4]
    assert not is_tile(bad[0])


def test_drag_keeps_edge_vertex_on_its_edge_and_carries_its_twin():
    # [0, 0.5, 0] dragged to x=0.3 stays on x=0; its twin follows in y, z.
    moves = dict(linked_moves(DIAMONDS[0], 3, [0.3, 0.6, 0.2], lock=True))
    assert moves == {3: [0, 0.6, 0.2], 5: [1, 0.6, 0.2]}
    moved = copy.deepcopy(DIAMONDS)
    for k, p in moves.items():
        moved[0][k] = p
    assert tile_problems(moved) == ([], [])


def test_corner_carries_all_three_other_corners_in_z():
    moves = dict(linked_moves(DIAMONDS[0], 6, [0.2, 0.2, 0.5], lock=True))
    assert moves == {6: [0, 0, 0.5], 0: [0, 1, 0.5], 2: [1, 1, 0.5], 8: [1, 0, 0.5]}


def test_interior_vertex_is_clamped_to_the_unit_square_alone():
    assert linked_moves(DIAMONDS[0], 4, [1.4, -0.2, 3], lock=True) == [(4, [1, 0, 3])]


def test_typed_value_is_taken_as_given():
    assert linked_moves(DIAMONDS[0], 3, [0.3, 0.5, 0], lock=False)[0] == (3, [0.3, 0.5, 0])
