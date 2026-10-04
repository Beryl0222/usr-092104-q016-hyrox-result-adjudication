import json
import unittest
from pathlib import Path

from src.envelope import AGGREGATE_TYPES, EVENT_AGGREGATE, EVENT_TYPES, validate_envelope

ROOT = Path(__file__).parents[1]


class EnvelopeSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )

    def test_event_types_match_contract(self):
        declared = set(self.schema["properties"]["event_type"]["enum"])
        self.assertEqual(declared, set(EVENT_TYPES))

    def test_aggregate_types_match_contract(self):
        declared = set(self.schema["properties"]["aggregate_type"]["enum"])
        self.assertEqual(declared, set(AGGREGATE_TYPES))

    def test_pairing_table_is_consistent(self):
        for event_type, aggregates in EVENT_AGGREGATE.items():
            self.assertIn(event_type, EVENT_TYPES)
            for aggregate in aggregates:
                self.assertIn(aggregate, AGGREGATE_TYPES)

    def test_sample_event_validates(self):
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_envelope(sample), [])

    def test_sample_stream_validates(self):
        events = json.loads((ROOT / "data" / "sample_events.json").read_text(encoding="utf-8"))
        for event in events:
            self.assertEqual(validate_envelope(event), [], event["event_id"])


if __name__ == "__main__":
    unittest.main()
