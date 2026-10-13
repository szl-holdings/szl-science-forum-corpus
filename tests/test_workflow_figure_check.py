"""Build 3: bounded canonical CSV-to-SVG figure package checks."""

import itertools
import json
import re
import tempfile
import unittest
from pathlib import Path

from szl_forum_corpus.workflows import figure_check as figures
from szl_forum_corpus.workflows.contracts import WorkflowError
from szl_forum_corpus.workflows.store import Context, Store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "science_workflows" / "figure_check"
OPERATOR = Context("operator-1", "tenant-a", "operator")
VIEWER = Context("viewer-1", "tenant-a", "viewer")
OTHER = Context("operator-9", "tenant-b", "operator")


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def members(**overrides):
    contents = {"spec": text("spec.json"), "data": text("data.csv")}
    contents.update(overrides)
    return [{"role": role, "content": content} for role, content in contents.items()
            if content is not None]


class FigureCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "workflows.sqlite3")
        self.ids = (f"{number:032x}" for number in itertools.count(1))
        self.expected = json.loads(text("expected.json"))

    def tearDown(self):
        self.directory.cleanup()

    def rid(self):
        return next(self.ids)

    def register(self, bundle_id, member_list, context=OPERATOR, **extra):
        payload = {"bundle_id": bundle_id, "evidence_kind": "synthetic",
                   "provenance": "tests/fixtures/science_workflows/PROVENANCE.md",
                   "members": member_list}
        payload.update(extra)
        return figures.register_bundle(self.store, context, self.rid(), payload)

    def bundle(self, bundle_id):
        return self.store.get(OPERATOR, figures.BUNDLE, bundle_id)

    def check(self, bundle_id, check_id):
        revision = self.bundle(bundle_id)["revision"]
        figures.check_bundle(self.store, OPERATOR, self.rid(), bundle_id, revision, check_id)
        return self.store.get(OPERATOR, figures.CHECK, check_id)

    def rendered(self):
        if not hasattr(self, "_base"):
            self.register("fig-1", members())
            figures.render_bundle(self.store, OPERATOR, self.rid(), "fig-1", "fig-1.r1")
            package = figures.package(self.store, OPERATOR, "fig-1.r1")
            self._base = (package["members"]["svg"]["content"],
                          package["members"]["sidecar"]["content"])
        return self._base

    def mutated(self, bundle_id, svg=None, sidecar=None):
        base_svg, base_sidecar = self.rendered()
        self.register(bundle_id, members(svg=base_svg if svg is None else svg(base_svg),
                                         sidecar=base_sidecar if sidecar is None
                                         else sidecar(base_sidecar)),
                      derived_from="fig-1.r1")
        return self.check(bundle_id, f"chk-{bundle_id}")

    def differences(self, check, member):
        entry = next(item for item in check["mismatches"] if item["member"] == member)
        return entry["differences"]

    # ---- baseline ---------------------------------------------------------------------------

    def test_rendered_baseline_matches_hand_derived_geometry_and_passes(self):
        svg, sidecar = self.rendered()
        for row, (x, y) in self.expected["pixels"].items():
            self.assertIn(f'<circle data-row="{row}" cx="{x}" cy="{y}" r="3" fill="#1764ab"/>', svg)
        self.assertIn(self.expected["caption_text"], svg)
        plotted = json.loads(sidecar)
        self.assertEqual(plotted["summary"], self.expected["summary"])
        self.assertEqual(plotted["points"], self.expected["sidecar_points"])
        check = self.check("fig-1.r1", "chk-1")
        self.assertEqual(check["execution"], "PASSED")
        self.assertEqual(check["statement"], "Canonical data-to-render checks passed.")
        self.assertEqual(check["plotted_rows"], self.expected["plotted_rows"])
        self.assertEqual(check["canonical_format"], "canonical")
        self.assertEqual(check["states"]["human_publication_review"], "NOT_PERFORMED")
        self.assertNotIn("publication_eligible", json.dumps(check))
        self.assertIn("not the number of independent experimental units", check["limitations"][0])
        self.assertEqual(self.bundle("fig-1.r1")["latest_check"]["execution"], "PASSED")

    def test_rendering_never_changes_the_source_bundle(self):
        self.rendered()
        source = self.bundle("fig-1")
        self.assertEqual(sorted(source["inventory"]), ["data", "spec"])
        self.assertEqual(source["revision"], 1)
        self.assertEqual(self.bundle("fig-1.r1")["derived_from"], "fig-1")

    # ---- mutations reject for their intended reason -------------------------------------------

    def test_moved_coordinate_is_localized_to_its_row(self):
        check = self.mutated("m-coord", svg=lambda s: s.replace('cy="244.179104"', 'cy="240.000000"'))
        self.assertEqual(check["execution"], "FAILED")
        svg = next(item for item in check["mismatches"] if item["member"] == "svg")
        self.assertEqual(svg["kernel_reason"], "SVG is not the canonical generated artifact")
        self.assertIn({"scope": "svg.point", "locator": {"row": "s3"}, "field": "cy",
                       "expected": "244.179104", "observed": "240.000000"},
                      svg["differences"])

    def test_removed_point_is_reported_missing(self):
        check = self.mutated("m-remove", svg=lambda s: re.sub(r'<circle data-row="s4"[^>]*/>\n', "", s))
        self.assertIn({"scope": "svg.point", "locator": {"row": "s4"}, "issue": "point_missing"},
                      self.differences(check, "svg"))

    def test_changed_axis_and_series_labels_are_reported(self):
        check = self.mutated("m-labels", svg=lambda s: s.replace("dose_mg (mg)", "dose_mg (g)")
                             .replace('data-kind="line"', 'data-kind="scatter"'))
        differences = self.differences(check, "svg")
        self.assertIn({"scope": "svg.label", "locator": {"label": "x-unit"}, "field": "text",
                       "expected": "dose_mg (mg)", "observed": "dose_mg (g)"}, differences)
        self.assertIn({"scope": "svg.series", "field": "data-kind", "expected": "line",
                       "observed": "scatter"}, differences)

    def test_altered_sidecar_fails_even_with_recomputed_inventory_digests(self):
        check = self.mutated("m-sidecar", sidecar=lambda s: s.replace('"y":"2.8"', '"y":"2.9"'))
        self.assertEqual(check["execution"], "FAILED")
        sidecar = next(item for item in check["mismatches"] if item["member"] == "sidecar")
        self.assertEqual(sidecar["kernel_reason"], "plotted data differs")
        self.assertIn({"scope": "sidecar.point", "locator": {"row": "s2"}, "field": "y",
                       "expected": "2.8", "observed": "2.9"}, sidecar["differences"])
        self.assertIn({"scope": "sidecar.summary", "locator": {"field": "n"}, "expected": 5,
                       "observed": 4},
                      self.differences(self.mutated(
                          "m-summary", sidecar=lambda s: s.replace('"n":5', '"n":4')), "sidecar"))
        # The inventory digest is computed over the mutated bytes, so it cannot rescue the check.
        self.assertEqual(len(self.bundle("m-sidecar")["inventory"]["sidecar"]["digest"]["value"]), 64)

    def test_equivalent_reformatting_is_unsupported_not_passed(self):
        check = self.mutated("m-format", svg=lambda s: s.replace("\n", "\n  "))
        self.assertEqual(check["execution"], "FAILED")
        self.assertEqual(check["canonical_format"], "unsupported_formatting")
        self.assertEqual(self.differences(check, "svg"),
                         [{"scope": "svg.document", "issue": "non_canonical_formatting"}])

    def test_malformed_svg_dtd_and_external_resources_fail(self):
        cases = {
            "m-malformed": (lambda s: s[:-8], "malformed_xml"),
            "m-dtd": (lambda s: '<!DOCTYPE svg [<!ENTITY a "b">]>' + s,
                      "declaration_or_processing_instruction"),
            "m-external": (lambda s: s.replace("</svg>", '<image href="https://example.invalid/x.png"/></svg>'),
                           "external_reference"),
            "m-script": (lambda s: s.replace("</svg>", "<script>1</script></svg>"),
                         "unexpected_element"),
        }
        for bundle_id, (mutate, issue) in cases.items():
            with self.subTest(bundle_id=bundle_id):
                check = self.mutated(bundle_id, svg=mutate)
                self.assertEqual(check["execution"], "FAILED")
                self.assertIn(issue, [item.get("issue") for item in self.differences(check, "svg")])
        dtd = self.store.get(OPERATOR, figures.CHECK, "chk-m-dtd")
        self.assertEqual(next(item for item in dtd["mismatches"] if item["member"] == "svg")
                         ["kernel_reason"], "SVG declarations and processing instructions refused")

    # ---- admission and caption refusals ----------------------------------------------------

    def test_invalid_caption_admits_inputs_but_refuses_render_and_fails_check(self):
        spec = text("spec.json").replace('"n": 5', '"n": 6')
        svg, sidecar = self.rendered()
        self.register("cap", members(spec=spec))
        self.assertEqual(self.bundle("cap")["contract_state"], "CAPTION_MISMATCH")
        with self.assertRaises(WorkflowError):
            figures.render_bundle(self.store, OPERATOR, self.rid(), "cap", "cap.r1")
        with self.assertRaises(WorkflowError) as caught:
            self.bundle("cap.r1")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.register("cap2", members(spec=spec, svg=svg, sidecar=sidecar))
        check = self.check("cap2", "chk-cap2")
        self.assertEqual(check["execution"], "FAILED")
        self.assertEqual(check["mismatches"][0]["kernel_reason"],
                         "caption assertions disagree with the CSV")
        self.assertEqual(check["mismatches"][0]["localization"], "unavailable")

    def test_unsupported_or_invalid_inputs_are_refused(self):
        bad = {
            "nonfinite": members(data=text("data.csv").replace("4.1", "NaN")),
            "infinite": members(data=text("data.csv").replace("4.1", "inf")),
            "bar": members(spec=text("spec.json").replace('"line"', '"bar"')),
            "extra-spec-field": members(spec=text("spec.json").replace(
                '"kind"', '"style": "bold", "kind"')),
            "duplicate-row": members(data=text("data.csv").replace("s5,", "s4,")),
            "float-caption": members(spec=text("spec.json").replace('"x_min": "0"', '"x_min": 0')),
        }
        for bundle_id, member_list in bad.items():
            with self.subTest(bundle_id=bundle_id):
                with self.assertRaises(WorkflowError):
                    self.register(bundle_id, member_list)
        self.assertEqual(self.store.list(OPERATOR, figures.BUNDLE), [])

    def test_missing_duplicate_and_unknown_members_are_refused(self):
        base = members()
        cases = [base[:1], base + [base[1]], base + [{"role": "readme", "content": "x"}],
                 base + [{"role": "svg", "content": "<svg/>"}],
                 base + [{"role": "data", "content": "x", "path": "../etc"}]]
        for number, member_list in enumerate(cases):
            with self.subTest(number=number):
                with self.assertRaises(WorkflowError):
                    self.register(f"bad{number}", member_list)

    def test_member_size_caps_are_enforced_before_parsing(self):
        with self.assertRaises(WorkflowError) as caught:
            self.register("big", members(data="a" * (figures.LIMITS["data"] + 1)))
        self.assertEqual(caught.exception.code, "TOO_LARGE")

    def test_check_needs_rendered_or_supplied_outputs(self):
        self.register("fig-1", members())
        with self.assertRaises(WorkflowError):
            figures.check_bundle(self.store, OPERATOR, self.rid(), "fig-1", 1, "chk-x")

    # ---- versions and history --------------------------------------------------------------

    def test_failed_recheck_never_shows_a_previous_pass_on_the_new_version(self):
        self.rendered()
        self.check("fig-1.r1", "chk-pass")
        self.mutated("fig-1.r2", svg=lambda s: s.replace('cx="127.500000"', 'cx="128.000000"'))
        self.assertEqual(self.bundle("fig-1.r2")["latest_check"]["execution"], "FAILED")
        self.assertEqual(self.bundle("fig-1.r1")["latest_check"]["check_id"], "chk-pass")
        self.register("fig-1.r3", members(), derived_from="fig-1.r2")
        self.assertIsNone(self.bundle("fig-1.r3")["latest_check"])

    def test_checks_are_immutable_and_scoped(self):
        self.rendered()
        self.check("fig-1.r1", "chk-1")
        with self.assertRaises(WorkflowError) as caught:
            figures.check_bundle(self.store, OPERATOR, self.rid(), "fig-1.r1",
                                 self.bundle("fig-1.r1")["revision"], "chk-1")
        self.assertEqual(caught.exception.code, "EXISTS")
        with self.assertRaises(WorkflowError) as caught:
            figures.package(self.store, OTHER, "fig-1.r1")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(WorkflowError) as caught:
            self.register("viewer", members(), context=VIEWER)
        self.assertEqual(caught.exception.code, "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
