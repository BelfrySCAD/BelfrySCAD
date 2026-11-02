from belfryscad.engine.renderer import light_from_pointer


def test_light_follows_the_pointer_towards_the_viewer():
    assert light_from_pointer(400, 300, 800, 600) == (0.0, 0.0, 1.0)      # centre: head-on
    x, y, z = light_from_pointer(800, 0, 800, 600)                         # top right corner
    assert x > 1 and y == 1.0 and z == 1.0
    assert light_from_pointer(100, 550, 800, 600)[:2] == (-1.0, -5 / 6)    # bottom left, y up
