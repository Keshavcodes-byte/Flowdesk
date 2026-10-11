import pytest

from app.engine.contracts import WorkflowInputError
from app.engine.transformations import apply_transformations, validate_transform_config


def test_cleaning_trims_strings_and_removes_rows_that_become_blank():
    headers, rows, numbers = apply_transformations(
        ["A", "B"], [["  ", "\t"], [" text ", " 2 "]], [2, 3],
        [{"type": "clean_data", "config": {"trim_strings": True, "remove_blank_rows": True}}],
    )
    assert headers == ["A", "B"]
    assert rows == [["text", "2"]]
    assert numbers == [3]


def test_column_mapping_checks_source_columns_and_conflicts():
    headers, rows, numbers = apply_transformations(
        ["Item", "Units"], [[" Pen ", "2"]], [4],
        [{"type": "map_columns", "config": {"mapping": {"Item": "Product", "Units": "Quantity"}}}],
    )
    assert headers == ["Product", "Quantity"]
    assert rows == [[" Pen ", "2"]]
    assert numbers == [4]

    with pytest.raises(WorkflowInputError, match="missing source column"):
        apply_transformations(["Item"], [["Pen"]], [2], [
            {"type": "map_columns", "config": {"mapping": {"Missing": "Product"}}}
        ])
    with pytest.raises(WorkflowInputError, match="conflicts"):
        apply_transformations(["Item", "Product"], [["Pen", "Pencil"]], [2], [
            {"type": "map_columns", "config": {"mapping": {"Item": "Product"}}}
        ])


@pytest.mark.parametrize(
    ("operation", "expected"),
    [("add", 8.0), ("subtract", 2.0), ("multiply", 15.0), ("divide", 1.6666666666666667)],
)
def test_calculated_field_supports_explicit_arithmetic(operation, expected):
    headers, rows, _ = apply_transformations(
        ["A", "B"], [["5", "3"]], [2],
        [{"type": "calculate_field", "config": {
            "name": "Result", "operation": operation,
            "left_column": "A", "right_column": "B",
        }}],
    )
    assert headers == ["A", "B", "Result"]
    assert rows[0][2] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("right", "message"),
    [("0", "divide by zero"), ("not-a-number", "finite number"), ("Infinity", "finite number")],
)
def test_calculated_field_rejects_zero_and_invalid_numeric_inputs(right, message):
    with pytest.raises(WorkflowInputError, match=message):
        apply_transformations(
            ["A", "B"], [["5", right]], [8],
            [{"type": "calculate_field", "config": {
                "name": "Result", "operation": "divide",
                "left_column": "A", "right_column": "B",
            }}],
        )


@pytest.mark.parametrize(
    ("step_type", "config"),
    [
        ("clean_data", {"trim_strings": True}),
        ("clean_data", {"trim_strings": 1, "remove_blank_rows": False}),
        ("map_columns", {"mapping": {"A": "B", "C": "b"}}),
        ("map_columns", {"mapping": {"A": "B"}, "extra": True}),
        ("calculate_field", {"name": "X", "operation": "eval", "left_column": "A", "right_column": "B"}),
    ],
)
def test_transform_configurations_are_strict(step_type, config):
    with pytest.raises(WorkflowInputError):
        validate_transform_config(step_type, config)


def test_transformations_do_not_mutate_original_rows_and_can_chain_calculations():
    original = [["2", "3"]]
    headers, rows, _ = apply_transformations(
        ["A", "B"], original, [2], [
            {"type": "calculate_field", "config": {
                "name": "Sum", "operation": "add", "left_column": "A", "right_column": "B",
            }},
            {"type": "calculate_field", "config": {
                "name": "Double", "operation": "multiply", "left_column": "Sum", "right_column": "B",
            }},
        ],
    )
    assert original == [["2", "3"]]
    assert headers == ["A", "B", "Sum", "Double"]
    assert rows == [["2", "3", 5.0, 15.0]]
