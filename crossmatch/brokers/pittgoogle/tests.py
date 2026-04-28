"""Tests for the Pitt-Google reliability filter UDF builder.

The repo does not yet have a test runner configured; these tests are
written against the stdlib `unittest` framework so they can be exercised
via:

    python -m unittest crossmatch.brokers.pittgoogle.tests

Predicate-behavior tests (does the UDF actually drop alerts whose latest
diaSource has reliability < threshold, etc.) need a JavaScript runtime
to execute the SMT UDF source. Those tests are listed below as skipped
TODOs; they should be implemented once a JS-execution harness is added
(Node.js subprocess, `js2py`, or a real Pub/Sub integration test).
"""
import unittest

from .consumer import _UDF_FUNCTION_NAME, _build_reliability_udf


class BuildReliabilityUdfStructureTests(unittest.TestCase):
    """Source-string assertions that don't require a JS runtime."""

    def test_includes_threshold_value(self):
        udf = _build_reliability_udf(0.6)
        self.assertIn('0.6', udf)

    def test_declares_named_function_with_constant_name(self):
        # pittgoogle's first-create path parses the function name from the
        # source via `function\s+([a-zA-Z0-9_]+)\s*\(`, so the declaration
        # must use the named-function form, not arrow or `const f = function`.
        udf = _build_reliability_udf(0.6)
        self.assertIn(f'function {_UDF_FUNCTION_NAME}(message, metadata)', udf)

    def test_returns_null_and_message(self):
        udf = _build_reliability_udf(0.6)
        self.assertIn('return null;', udf)
        self.assertIn('return message;', udf)

    def test_under_smt_size_limit(self):
        # Pub/Sub SMT UDF code-size limit is 20 KB.
        udf = _build_reliability_udf(0.6)
        self.assertLess(len(udf.encode('utf-8')), 20 * 1024)

    def test_threshold_interpolation_no_trailing_zeros(self):
        # repr() of the documented thresholds emits the expected literal.
        for threshold, expected in [
            (0.6, '0.6'),
            (0.75, '0.75'),
            (0.9, '0.9'),
            (0.0, '0.0'),
        ]:
            with self.subTest(threshold=threshold):
                udf = _build_reliability_udf(threshold)
                self.assertIn(expected, udf)

    def test_uses_inverted_comparison_for_nan_safety(self):
        # NaN < threshold is false in JS, so a `score < threshold` predicate
        # would let NaN pass. The builder must use `!(score >= threshold)`
        # (or equivalent) so NaN is dropped because NaN >= anything is false.
        udf = _build_reliability_udf(0.6)
        self.assertIn('!(score >= 0.6)', udf)

    def test_does_not_double_decode_utf8_payload(self):
        # Per Google's SMT UDF contract, message.data is delivered as a
        # UTF-8 string -- https://cloud.google.com/pubsub/docs/smts/udfs-overview
        # Wrapping it in atob() would throw on the first non-base64
        # character of the JSON payload (e.g. "{") and drop every
        # message via the try/catch fallback. Lock in the correct shape
        # so this can't silently regress.
        udf = _build_reliability_udf(0.6)
        self.assertNotIn('atob', udf)
        self.assertIn('JSON.parse(message.data)', udf)


@unittest.skip(
    'Predicate behavior requires a JS runtime (Node.js, js2py, or real '
    'Pub/Sub integration). Implement once a test harness is available. '
    'Scenarios to cover: payload reliability above/below/equal-to threshold; '
    '"0.7" string rejected by typeof check; null reliability dropped; '
    'missing diaSource dropped; non-JSON payload dropped via try/catch; '
    'NaN reliability dropped.'
)
class PredicateBehaviorTests(unittest.TestCase):
    """Placeholders for end-to-end UDF execution coverage."""


if __name__ == '__main__':
    unittest.main()
