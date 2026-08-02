"""Tests for the Customizer's pure-Python source-scanning/constraint-parsing
layer (`scan_parameters`, `_parse_literal`, `_parse_constraint`,
`write_back_value`) -- the Qt widget classes (`_VectorWidget`,
`_SliderWidget`, `CustomizerPane`, etc.) aren't covered here, consistent
with the rest of the test suite (no existing tests instantiate Qt widgets).

Written alongside a fix for: a `[min:step:max]` range comment on a *vector*
parameter had no effect -- `_make_widget` built a `_VectorWidget` without
ever passing it the parsed constraint, so every component spinbox always
used a hardcoded (-1e9, 1e9) range regardless of what the comment said.
`_parse_constraint` itself never cared whether the default was a scalar or
a list (confirmed below), so the bug was entirely in the widget-dispatch/
`_VectorWidget` layer, not the parsing layer these tests cover -- verified
separately via a throwaway Qt script per this project's convention."""
import json

from belfryscad.window.customizer import (
    _parse_literal, _format_value, _parse_constraint, _default_step,
    _coerce_option_value, _build_constraint,
    scan_parameters, write_back_value, insert_parameter, replace_parameter, delete_parameter,
    preset_path_for, load_presets, save_presets,
)


class TestParseLiteral:
    def test_bool(self):
        assert _parse_literal('true') is True
        assert _parse_literal('false') is False

    def test_int(self):
        assert _parse_literal('5') == 5
        assert isinstance(_parse_literal('5'), int)

    def test_float(self):
        assert _parse_literal('5.5') == 5.5

    def test_string(self):
        assert _parse_literal('"hello"') == 'hello'

    def test_vector(self):
        assert _parse_literal('[1, 2, 3]') == [1, 2, 3]

    def test_float_vector(self):
        assert _parse_literal('[1.5, 2.5]') == [1.5, 2.5]

    def test_invalid_returns_none(self):
        assert _parse_literal('some_var') is None


class TestFormatValue:
    def test_bool(self):
        assert _format_value(True) == 'true'
        assert _format_value(False) == 'false'

    def test_vector(self):
        assert _format_value([1, 2, 3]) == '[1, 2, 3]'

    def test_float_always_has_decimal(self):
        assert _format_value(5.0) == '5.0'


class TestParseConstraint:
    def test_bare_max_is_slider_from_zero(self):
        spec = _parse_constraint('[10]', 5)
        assert spec == {'type': 'slider', 'min': 0.0, 'max': 10.0, 'step': _default_step(5)}

    def test_min_max(self):
        spec = _parse_constraint('[2:8]', 5)
        assert spec['type'] == 'slider'
        assert spec['min'] == 2.0
        assert spec['max'] == 8.0

    def test_min_step_max(self):
        spec = _parse_constraint('[0:5:100]', 50)
        assert spec == {'type': 'slider', 'min': 0.0, 'max': 100.0, 'step': 5.0}

    def test_min_step_max_applies_regardless_of_default_type(self):
        # _parse_constraint doesn't special-case vector vs scalar defaults --
        # confirms the range-parsing itself was never the bug behind
        # "vector [min:step:max] doesn't apply constraints".
        scalar_spec = _parse_constraint('[0:5:100]', 50)
        vector_spec = _parse_constraint('[0:5:100]', [10, 20, 30])
        assert scalar_spec == vector_spec

    def test_dropdown(self):
        spec = _parse_constraint('[a, b, c]', 'a')
        assert spec == {'type': 'dropdown', 'options': [('a', 'a'), ('b', 'b'), ('c', 'c')]}

    def test_dropdown_with_labels(self):
        spec = _parse_constraint('[a:Alpha, b:Beta]', 'a')
        assert spec == {'type': 'dropdown', 'options': [('a', 'Alpha'), ('b', 'Beta')]}

    def test_bare_integer_on_string_is_maxlen(self):
        spec = _parse_constraint('20', 'hello')
        assert spec == {'type': 'string', 'maxlen': 20}

    def test_no_constraint(self):
        assert _parse_constraint('', 5) == {'type': 'default'}


class TestCoerceOptionValue:
    def test_int(self):
        assert _coerce_option_value('10') == 10
        assert isinstance(_coerce_option_value('10'), int)

    def test_float(self):
        assert _coerce_option_value('2.5') == 2.5

    def test_string(self):
        assert _coerce_option_value('sm') == 'sm'


class TestBuildConstraint:
    def test_slider_roundtrips_through_parse_constraint(self):
        text = _build_constraint('slider', min=0, max=100, step=5)
        assert text == '[0:5:100]'
        assert _parse_constraint(text, 50) == {'type': 'slider', 'min': 0.0, 'max': 100.0, 'step': 5.0}

    def test_dropdown_bare_when_label_matches_value(self):
        text = _build_constraint('dropdown', options=[('a', 'a'), ('b', 'b')])
        assert text == '[a, b]'

    def test_dropdown_with_labels_roundtrips(self):
        text = _build_constraint('dropdown', options=[('sm', 'Small'), ('lg', 'Large')])
        assert text == '[sm:Small, lg:Large]'
        assert _parse_constraint(text, 'sm') == {
            'type': 'dropdown', 'options': [('sm', 'Small'), ('lg', 'Large')],
        }

    def test_string_maxlen(self):
        assert _build_constraint('string', maxlen=20) == '20'

    def test_string_no_maxlen_is_empty(self):
        assert _build_constraint('string', maxlen=0) == ''

    def test_number_and_checkbox_are_empty(self):
        assert _build_constraint('number') == ''
        assert _build_constraint('checkbox') == ''


class TestScanParameters:
    def test_simple_assignment(self):
        params = scan_parameters('width = 10;\n')
        assert len(params) == 1
        assert params[0].name == 'width'
        assert params[0].default == 10

    def test_vector_with_range_constraint(self):
        source = 'pos = [0, 0, 0]; // [0:1:10]\n'
        params = scan_parameters(source)
        assert len(params) == 1
        assert params[0].name == 'pos'
        assert params[0].default == [0, 0, 0]
        assert params[0].constraint == '[0:1:10]'

    def test_description_comment_captured(self):
        source = '// Width of the box\nwidth = 10;\n'
        params = scan_parameters(source)
        assert params[0].description == 'Width of the box'

    def test_hidden_tab_suppresses_params(self):
        source = '/* [Hidden] */\nsecret = 1;\n'
        assert scan_parameters(source) == []

    def test_tab_group(self):
        source = '/* [Sizes] */\nwidth = 10;\n'
        params = scan_parameters(source)
        assert params[0].tab == 'Sizes'

    def test_nested_assignment_not_a_parameter(self):
        source = 'module foo() {\n  x = 5;\n}\n'
        assert scan_parameters(source) == []

    def test_dollar_variable_excluded(self):
        assert scan_parameters('$fn = 32;\n') == []


class TestWriteBackValue:
    def test_replaces_scalar(self):
        source = 'width = 10;\n'
        result = write_back_value(source, 'width', 20)
        assert result == 'width = 20;\n'

    def test_replaces_vector(self):
        source = 'pos = [0, 0, 0];\n'
        result = write_back_value(source, 'pos', [1, 2, 3])
        assert result == 'pos = [1, 2, 3];\n'

    def test_preserves_trailing_comment(self):
        source = 'width = 10; // [0:100]\n'
        result = write_back_value(source, 'width', 20)
        assert result == 'width = 20; // [0:100]\n'

    def test_unknown_name_returns_source_unchanged(self):
        source = 'width = 10;\n'
        assert write_back_value(source, 'nonexistent', 5) == source


class TestInsertParameter:
    def test_into_empty_file(self):
        result = insert_parameter('', 'width', 10, 'Width', 'Parameters', '')
        assert result == '// Width\nwidth = 10;\n'

    def test_with_constraint(self):
        result = insert_parameter('', 'width', 10, '', 'Parameters', '1:100')
        assert result == 'width = 10; // 1:100\n'

    def test_appends_after_last_param_in_same_default_tab(self):
        source = 'a = 1;\nb = 2;\n'
        result = insert_parameter(source, 'c', 3, '', 'Parameters', '')
        assert result == 'a = 1;\nb = 2;\nc = 3;\n'

    def test_new_named_tab_appends_with_header(self):
        source = 'a = 1;\n'
        result = insert_parameter(source, 'b', 2, '', 'Sizes', '')
        assert result == 'a = 1;\n\n/* [Sizes] */\nb = 2;\n'

    def test_second_param_in_same_named_tab_no_new_header(self):
        source = 'a = 1;\n\n/* [Sizes] */\nb = 2;\n'
        result = insert_parameter(source, 'c', 3, '', 'Sizes', '')
        assert result == 'a = 1;\n\n/* [Sizes] */\nb = 2;\nc = 3;\n'

    def test_default_tab_param_inserted_before_first_header(self):
        # A headerless ("Parameters") param can't go after a tab-group
        # header started earlier in the file -- it would be misread as
        # belonging to that tab (scan_parameters has no "reset" syntax).
        source = '/* [Sizes] */\nb = 2;\n'
        result = insert_parameter(source, 'a', 1, '', 'Parameters', '')
        assert result == 'a = 1;\n/* [Sizes] */\nb = 2;\n'

    def test_global_tab_gets_explicit_header(self):
        source = 'a = 1;\n'
        result = insert_parameter(source, 'g', 5, '', 'Global', '')
        assert result == 'a = 1;\n\n/* [Global] */\ng = 5;\n'


class TestReplaceParameter:
    def test_replaces_value_and_constraint(self):
        source = 'width = 10; // [0:50]\n'
        result = replace_parameter(source, 'width', 20, '', '0:100')
        assert result == 'width = 20; // 0:100\n'

    def test_removes_constraint_when_empty(self):
        source = 'width = 10; // [0:50]\n'
        result = replace_parameter(source, 'width', 20, '', '')
        assert result == 'width = 20;\n'

    def test_updates_existing_description(self):
        source = '// Old desc\nwidth = 10;\n'
        result = replace_parameter(source, 'width', 10, 'New desc', '')
        assert result == '// New desc\nwidth = 10;\n'

    def test_adds_description_where_none_existed(self):
        source = 'width = 10;\n'
        result = replace_parameter(source, 'width', 10, 'New desc', '')
        assert result == '// New desc\nwidth = 10;\n'

    def test_removes_description_when_cleared(self):
        source = '// Old desc\nwidth = 10;\n'
        result = replace_parameter(source, 'width', 10, '', '')
        assert result == 'width = 10;\n'

    def test_unknown_name_returns_source_unchanged(self):
        source = 'width = 10;\n'
        assert replace_parameter(source, 'nonexistent', 5, '', '') == source


class TestDeleteParameter:
    def test_removes_assignment_line(self):
        source = 'a = 1;\nb = 2;\n'
        assert delete_parameter(source, 'a') == 'b = 2;\n'

    def test_removes_description_line_too(self):
        source = '// A desc\na = 1;\nb = 2;\n'
        assert delete_parameter(source, 'a') == 'b = 2;\n'

    def test_leaves_tab_header_in_place(self):
        source = '/* [Sizes] */\na = 1;\nb = 2;\n'
        assert delete_parameter(source, 'a') == '/* [Sizes] */\nb = 2;\n'

    def test_unknown_name_returns_source_unchanged(self):
        source = 'width = 10;\n'
        assert delete_parameter(source, 'nonexistent') == source


class TestPresetPathFor:
    def test_swaps_scad_extension_for_json(self):
        assert preset_path_for('/tmp/model.scad') == '/tmp/model.json'

    def test_no_extension(self):
        assert preset_path_for('/tmp/model') == '/tmp/model.json'


class TestPresetIO:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_presets(str(tmp_path / 'nope.json')) == {}

    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / 'model.json')
        presets = {
            'Small': {'width': 5, 'name': 'a', 'flag': True, 'pos': [1, 2, 3]},
            'Large': {'width': 50.5, 'name': 'b', 'flag': False, 'pos': [10, 20, 30]},
        }
        save_presets(path, presets)
        assert load_presets(path) == presets

    def test_values_stored_as_scad_literal_syntax(self, tmp_path):
        path = str(tmp_path / 'model.json')
        save_presets(path, {'Preset1': {'width': 5, 'name': 'hi', 'flag': True}})
        data = json.loads((tmp_path / 'model.json').read_text())
        assert data['parameterSets']['Preset1'] == {
            'width': '5', 'name': '"hi"', 'flag': 'true',
        }
        assert data['fileFormatVersion'] == '1'

    def test_corrupt_json_returns_empty(self, tmp_path):
        path = tmp_path / 'model.json'
        path.write_text('not json{{{')
        assert load_presets(str(path)) == {}

    def test_unparseable_values_skipped(self, tmp_path):
        path = tmp_path / 'model.json'
        path.write_text(json.dumps({
            'parameterSets': {'P': {'good': '5', 'bad': 'some_var'}},
        }))
        assert load_presets(str(path)) == {'P': {'good': 5}}
